//! MedBridge 데스크톱 셸.
//!
//! 역할: 창 표시, FastAPI sidecar의 시작·준비 확인·감시·종료, 앱 데이터 경로·동적 포트·토큰 전달,
//! 업데이트 실행, 오류 보고서 저장. sidecar는 127.0.0.1의 임의 포트에 바인딩되고,
//! 토큰 없는 요청은 거부한다. 포트·토큰·내부 경로는 GUI에 표시하지 않는다.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;

#[cfg(windows)]
#[allow(non_snake_case)]
mod process_tree {
    use std::ffi::{c_void, OsStr};
    use std::io;
    use std::mem::{size_of, zeroed};
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::io::AsRawHandle;
    use std::process::{Child, Command};
    use std::ptr::null;

    type Handle = *mut c_void;

    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: i32 = 9;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;

    #[repr(C)]
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        other_transfer_count: u64,
    }

    #[repr(C)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    struct ExtendedLimitInformation {
        basic_limit_information: BasicLimitInformation,
        io_info: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        fn SetInformationJobObject(
            job: Handle,
            information_class: i32,
            information: *const c_void,
            information_length: u32,
        ) -> i32;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        fn CloseHandle(object: Handle) -> i32;
    }

    /// Windows Job Object whose last-handle close terminates every assigned descendant.
    ///
    /// The raw handle is stored as an integer so this guard can live in Tauri managed
    /// state (`Send + Sync`). It is converted back only at the FFI boundary.
    pub(super) struct ProcessTreeGuard {
        handle: isize,
        name: String,
    }

    impl ProcessTreeGuard {
        pub(super) fn new() -> io::Result<Self> {
            let name = format!("Local\\MedBridgeSidecar-{}", uuid::Uuid::new_v4());
            let wide_name: Vec<u16> = OsStr::new(&name).encode_wide().chain(Some(0)).collect();
            let handle = unsafe { CreateJobObjectW(null(), wide_name.as_ptr()) };
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }

            let mut limits: ExtendedLimitInformation = unsafe { zeroed() };
            limits.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let configured = unsafe {
                SetInformationJobObject(
                    handle,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                    &limits as *const ExtendedLimitInformation as *const c_void,
                    size_of::<ExtendedLimitInformation>() as u32,
                )
            };
            if configured == 0 {
                let error = io::Error::last_os_error();
                unsafe {
                    CloseHandle(handle);
                }
                return Err(error);
            }

            Ok(Self {
                handle: handle as isize,
                name,
            })
        }

        pub(super) fn configure_command(&self, command: &mut Command) {
            // The frozen Python child opens this same named job and joins itself.
            // This repairs the case where the bootloader creates its Python child
            // before Rust assigns the bootloader below. There is still an extremely
            // small startup interval between Command::spawn and assign during which
            // an abrupt shell crash can leave the not-yet-assigned bootloader alive.
            command.env("MEDBRIDGE_WINDOWS_JOB_NAME", &self.name);
        }

        pub(super) fn assign(&self, child: &Child) -> io::Result<()> {
            let assigned = unsafe {
                AssignProcessToJobObject(self.handle as Handle, child.as_raw_handle() as Handle)
            };
            if assigned == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        }
    }

    impl Drop for ProcessTreeGuard {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.handle as Handle);
            }
        }
    }
}

#[cfg(not(windows))]
mod process_tree {
    use std::io;
    use std::process::{Child, Command};

    pub(super) struct ProcessTreeGuard;

    impl ProcessTreeGuard {
        pub(super) fn new() -> io::Result<Self> {
            Ok(Self)
        }

        pub(super) fn configure_command(&self, _command: &mut Command) {}

        pub(super) fn assign(&self, _child: &Child) -> io::Result<()> {
            Ok(())
        }
    }
}

use process_tree::ProcessTreeGuard;

#[derive(Clone, Copy, PartialEq)]
enum Startup {
    Starting,
    Ready,
    Failed,
}

struct SidecarState {
    port: u16,
    token: String,
    child: Mutex<Option<Child>>,
    process_tree: Mutex<Option<ProcessTreeGuard>>,
    startup: Mutex<Startup>,
}

#[derive(serde::Serialize)]
struct SidecarInfo {
    base: String,
    token: String,
}

/// sidecar가 실제로 바인딩된 포트로부터 base URL을 만드는 유일한 지점.
/// GUI(sidecar_info)와 오류 보고서(fetch_error_report)가 이 한 함수만 거치게 해
/// 두 경로의 포트가 어긋날 여지를 없앤다.
fn sidecar_base_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

/// GUI가 sidecar 주소·토큰을 조회하는 유일한 통로 (화면에는 표시하지 않는다)
#[tauri::command]
fn sidecar_info(state: tauri::State<SidecarState>) -> SidecarInfo {
    SidecarInfo {
        base: sidecar_base_url(state.port),
        token: state.token.clone(),
    }
}

/// 준비 화면용 상태: "starting" | "ready" | "failed"
#[tauri::command]
fn sidecar_status(state: tauri::State<SidecarState>) -> &'static str {
    match *state.startup.lock().unwrap() {
        Startup::Starting => "starting",
        Startup::Ready => "ready",
        Startup::Failed => "failed",
    }
}

/// 오류 보고서 zip 생성 — 저장 위치는 사용자가 대화상자로 선택한다.
/// PDF 원문·토큰·DB 전체는 포함하지 않는다 (sidecar가 정제한 데이터만 사용).
/// async 커맨드로 워커에서 돌린다 — 동기 커맨드는 메인 스레드에서 실행돼
/// 네트워크 대기(최대 10초) 동안 창 전체(입력·렌더링)가 얼어붙는다.
#[tauri::command]
async fn save_error_report(
    app: tauri::AppHandle,
    state: tauri::State<'_, SidecarState>,
) -> Result<bool, String> {
    use tauri_plugin_dialog::DialogExt;

    // 저장 위치부터 고른다 — 취소하면 네트워크 대기를 아예 지불하지 않는다.
    let picked = app
        .dialog()
        .file()
        .set_file_name("medbridge-error-report.zip")
        .blocking_save_file();
    let Some(path) = picked else {
        return Ok(false); // 사용자가 취소
    };
    let path = path.into_path().map_err(|e| e.to_string())?;

    let startup_failed = matches!(*state.startup.lock().unwrap(), Startup::Failed);
    let port = state.port;
    let token = state.token.clone();
    let meta = serde_json::json!({
        "appVersion": app.package_info().version.to_string(),
        "os": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
    })
    .to_string();

    tauri::async_runtime::spawn_blocking(move || {
        let report_body = fetch_error_report(startup_failed, port, &token).unwrap_or_else(|e| {
            // sidecar가 죽어 있어도 최소 정보는 저장한다 (오류 문자열은 JSON 이스케이프)
            serde_json::json!({"error": "sidecar unavailable", "detail": e}).to_string()
        });
        let file = std::fs::File::create(&path).map_err(|e| e.to_string())?;
        let mut z = zip::ZipWriter::new(file);
        let opts: zip::write::SimpleFileOptions = Default::default();
        z.start_file("report.json", opts)
            .map_err(|e| e.to_string())?;
        z.write_all(report_body.as_bytes())
            .map_err(|e| e.to_string())?;
        z.start_file("app.json", opts).map_err(|e| e.to_string())?;
        z.write_all(meta.as_bytes()).map_err(|e| e.to_string())?;
        z.finish().map_err(|e| e.to_string())?;
        Ok(true)
    })
    .await
    .map_err(|e| e.to_string())?
}

fn fetch_error_report(startup_failed: bool, port: u16, token: &str) -> Result<String, String> {
    // 시작 자체가 실패한 상태에서는 원인이 이미 명확하므로, 원시 소켓 오류 대신
    // 사람이 읽을 수 있는 안내를 우선 반환한다 (죽은 sidecar에 연결 시도하지 않음).
    if startup_failed {
        return Err("sidecar를 시작하지 못해 오류 정보를 가져올 수 없습니다.".into());
    }
    let url = format!("{}/api/system/error-report", sidecar_base_url(port));
    ureq::get(&url)
        .set("X-MedBridge-Token", token)
        .timeout(Duration::from_secs(10))
        .call()
        .map_err(|e| e.to_string())?
        .into_string()
        .map_err(|e| e.to_string())
}

fn pick_free_port() -> u16 {
    // ponytail: OS가 고른 빈 포트를 즉시 반환 — 해제~기동 사이 짧은 선점 경쟁은
    // 준비 확인(HTTP /health 검증)으로 흡수한다. 문제가 실측되면 IPC 방식으로 교체.
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .expect("no free port available")
}

fn spawn_sidecar(
    app: &tauri::AppHandle,
    port: u16,
    token: &str,
) -> std::io::Result<(Child, ProcessTreeGuard)> {
    let app_data_dir = app.path().app_data_dir().expect("app data dir unavailable");
    std::fs::create_dir_all(&app_data_dir).ok();
    let process_tree = ProcessTreeGuard::new()?;

    let mut cmd = if cfg!(debug_assertions) {
        // 개발 모드: apps/api를 uv로 직접 실행
        let api_dir = std::env::current_dir()
            .unwrap()
            .join("../../api")
            .canonicalize()
            .expect("apps/api not found");
        let mut c = Command::new("uv");
        c.args([
            "run",
            "python",
            "sidecar_entry.py",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
        ])
        .current_dir(api_dir);
        c
    } else {
        // 패키징 모드: Tauri externalBin은 메인 실행 파일 옆에 논리 이름으로 설치된다
        let exe_dir = std::env::current_exe()
            .expect("current exe unavailable")
            .parent()
            .expect("exe dir unavailable")
            .to_path_buf();
        let name = if cfg!(windows) {
            "medbridge-sidecar.exe"
        } else {
            "medbridge-sidecar"
        };
        let mut c = Command::new(exe_dir.join(name));
        c.args(["--host", "127.0.0.1", "--port", &port.to_string()]);
        c
    };

    // 번들된 OCR 리소스 경로 전달 (없으면 sidecar가 PATH fallback)
    //
    // resource_dir()는 Windows에서 확장 길이 접두사(\\?\)가 붙은 경로를 돌려준다. 그대로
    // 넘기면 sidecar가 TESSDATA_PREFIX로 쓰고, tesseract는 거기에 "/eng.traineddata"를
    // 이어 붙인다. Win32는 \\?\ 경로를 정규화하지 않아 섞인 슬래시 때문에 파일을 못 열고,
    // 결과적으로 OCR 페이지가 전량 "Failed loading language" 로 실패한다(실기기 45/45).
    // sidecar도 방어적으로 벗기지만, 애초에 붙여 보내지 않는다.
    if let Ok(resource_dir) = app.path().resource_dir() {
        let ocr_dir = resource_dir.join("resources").join("ocr");
        if ocr_dir.is_dir() {
            let value = ocr_dir.to_string_lossy();
            let plain = value
                .strip_prefix(r"\\?\UNC\")
                .map(|rest| format!(r"\\{rest}"))
                .or_else(|| value.strip_prefix(r"\\?\").map(str::to_string))
                .unwrap_or_else(|| value.to_string());
            cmd.env("MEDBRIDGE_OCR_DIR", plain);
        }
    }
    cmd.env("MEDBRIDGE_APP_DATA_DIR", &app_data_dir)
        .env("MEDBRIDGE_API_TOKEN", token)
        .env("MEDBRIDGE_BOUND_PORT", port.to_string())
        .env(
            "APP_ENV",
            if cfg!(debug_assertions) {
                "development"
            } else {
                "production"
            },
        )
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    process_tree.configure_command(&mut cmd);

    let mut child = cmd.spawn()?;
    if let Err(error) = process_tree.assign(&child) {
        // A spawned-but-unassigned process would escape KILL_ON_JOB_CLOSE.
        // Stop the root, close the job to terminate any Python child that already
        // joined it, then reap the root before returning the startup failure.
        let _ = child.kill();
        drop(process_tree);
        let _ = child.wait();
        return Err(error);
    }
    Ok((child, process_tree))
}

/// sidecar가 실제로 우리 포트에 떠서 /health에 정상 응답할 때까지 대기.
/// 다른 프로세스가 포트를 선점한 경우 응답 검증에서 걸러진다.
fn wait_for_sidecar(
    port: u16,
    child: &Mutex<Option<Child>>,
    timeout: Duration,
) -> Result<(), String> {
    let deadline = std::time::Instant::now() + timeout;
    let addr = format!("127.0.0.1:{port}");
    while std::time::Instant::now() < deadline {
        if let Ok(mut guard) = child.lock() {
            if let Some(c) = guard.as_mut() {
                if let Ok(Some(status)) = c.try_wait() {
                    return Err(format!("sidecar exited early: {status}"));
                }
            }
        }
        if let Ok(mut stream) = TcpStream::connect(&addr) {
            let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
            let req = format!(
                "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
            );
            if stream.write_all(req.as_bytes()).is_ok() {
                let mut buf = String::new();
                let _ = stream.read_to_string(&mut buf);
                // 다른 로컬 프로세스가 포트를 선점한 경우를 걸러내기 위해
                // 200 응답 + 우리 sidecar의 버전 필드까지 확인한다
                let expected = format!("\"sidecarVersion\":\"{}\"", env!("CARGO_PKG_VERSION"));
                if buf.starts_with("HTTP/1.1 200")
                    && buf.contains("\"status\":\"ok\"")
                    && buf.contains(&expected)
                {
                    return Ok(());
                }
            }
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    Err("sidecar readiness timeout".into())
}

fn kill_sidecar(state: &SidecarState) {
    // Closing the last Job Object handle terminates the PyInstaller bootloader,
    // its Python child, and any OCR subprocesses as one process tree.
    // Recover poisoned mutexes so an unrelated panic cannot turn shutdown into a
    // process leak. The job must close before killing the bootloader directly;
    // otherwise PyInstaller's Python child could be orphaned.
    let mut process_tree = match state.process_tree.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    process_tree.take();
    drop(process_tree);

    let mut child = match state.child.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    if let Some(mut child) = child.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

pub fn run() {
    let port = pick_free_port();
    let token = uuid::Uuid::new_v4().to_string();

    let app = tauri::Builder::default()
        // 반드시 첫 플러그인으로 등록한다: 이미 실행 중인 창이 있으면 이 콜백이
        // 새 프로세스의 .setup() 이전에 개입해, sidecar를 다시 띄우지 못하게
        // 막는다(포트 예약·토큰 생성 자체는 이보다 앞서 일어나지만 무해하다).
        // 그렇지 않으면 두 번째 실행이 서로 다른 포트의 두 번째 sidecar를
        // 띄우면서, 첫 창은 죽은 sidecar를 보고 있는데 실제로 살아있는 sidecar는
        // 다른 포트라는 혼란스러운 상태가 된다.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .setup({
            let token = token.clone();
            move |app| {
                let (child, process_tree) = spawn_sidecar(&app.handle().clone(), port, &token)
                    .map_err(|e| format!("failed to start sidecar: {e}"))?;
                app.manage(SidecarState {
                    port,
                    token: token.clone(),
                    child: Mutex::new(Some(child)),
                    process_tree: Mutex::new(Some(process_tree)),
                    startup: Mutex::new(Startup::Starting),
                });
                // 창은 즉시 뜨고(준비 화면), 준비 확인은 백그라운드에서 진행한다
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    let state = handle.state::<SidecarState>();
                    // 마이그레이션 적용 기동은 수 GB DB의 백업 복사 + VACUUM을 포함해
                    // 느린 디스크에서 수 분이 걸릴 수 있다 — 120초로 자르면 그 기기는
                    // 업데이트 직후 매번 '시작 실패'가 뜨고 마이그레이션이 영영 끝나지
                    // 않는다. 죽은 sidecar는 try_wait로 즉시 감지되므로 넉넉히 잡는다.
                    let result = wait_for_sidecar(port, &state.child, Duration::from_secs(600));
                    match result {
                        Ok(()) => {
                            // 포트만 기록한다 — 토큰은 절대 로그에 남기지 않는다.
                            println!("sidecar ready at {}", sidecar_base_url(port));
                            *state.startup.lock().unwrap() = Startup::Ready;
                        }
                        Err(e) => {
                            eprintln!("sidecar startup failed (port {port}): {e}");
                            *state.startup.lock().unwrap() = Startup::Failed;
                            // A timed-out or unhealthy sidecar must not keep doing
                            // CPU/DB work behind the failed startup screen.
                            kill_sidecar(&state);
                        }
                    }
                });
                Ok(())
            }
        })
        .invoke_handler(tauri::generate_handler![
            sidecar_info,
            sidecar_status,
            save_error_report
        ])
        .build(tauri::generate_context!())
        .expect("error while building MedBridge");

    app.run(|app_handle, event| {
        // 창 파괴·앱 종료 어느 경로로 끝나도 sidecar를 정리한다
        // Windows에서는 시작 시 할당이 끝난 뒤 Job Object의
        // KILL_ON_JOB_CLOSE가 비정상 종료도 함께 봉쇄한다.
        match event {
            tauri::RunEvent::WindowEvent {
                event: tauri::WindowEvent::Destroyed,
                ..
            }
            | tauri::RunEvent::Exit => {
                if let Some(state) = app_handle.try_state::<SidecarState>() {
                    kill_sidecar(&state);
                }
            }
            _ => {}
        }
    });
}

#[cfg(test)]
mod tests {
    use super::sidecar_base_url;
    #[cfg(windows)]
    use super::ProcessTreeGuard;
    #[cfg(windows)]
    use std::process::{Command, Stdio};
    #[cfg(windows)]
    use std::time::{Duration, Instant};

    // sidecar_info(GUI)와 fetch_error_report(오류 보고서)가 같은 함수를 거치므로,
    // 이 테스트가 통과하는 한 두 경로가 서로 다른 포트를 가리킬 수 없다.
    #[test]
    fn base_url_embeds_the_given_port_faithfully() {
        assert_eq!(sidecar_base_url(3367), "http://127.0.0.1:3367");
        assert_eq!(sidecar_base_url(8765), "http://127.0.0.1:8765");
        assert_eq!(sidecar_base_url(0), "http://127.0.0.1:0");
    }

    #[test]
    fn base_url_never_hardcodes_a_fixed_port() {
        // 회귀 방지: 함수가 인자를 무시하고 고정값을 반환하면 이 비교가 실패한다.
        assert_ne!(sidecar_base_url(1234), sidecar_base_url(5678));
    }

    #[cfg(windows)]
    #[test]
    fn windows_process_tree_guard_kills_an_assigned_process_when_closed() {
        // This exercises the FFI layout, access rights, assignment, and the actual
        // KILL_ON_JOB_CLOSE behavior without creating an untracked descendant.
        let guard = ProcessTreeGuard::new().expect("Windows Job Object must be available");
        let mut command = Command::new("ping.exe");
        command
            .args(["-n", "30", "127.0.0.1"])
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        guard.configure_command(&mut command);
        let mut child = command
            .spawn()
            .expect("long-running Windows process must start");
        guard
            .assign(&child)
            .expect("process must be assignable to the Job Object");
        assert!(
            child
                .try_wait()
                .expect("process state must be readable")
                .is_none(),
            "test process exited before KILL_ON_JOB_CLOSE could be exercised"
        );

        let started = Instant::now();
        drop(guard);
        child.wait().expect("terminated process must be reapable");
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "closing the Job Object did not terminate its assigned process promptly"
        );
    }
}
