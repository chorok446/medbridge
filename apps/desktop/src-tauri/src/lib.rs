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
    startup: Mutex<Startup>,
}

#[derive(serde::Serialize)]
struct SidecarInfo {
    base: String,
    token: String,
}

/// GUI가 sidecar 주소·토큰을 조회하는 유일한 통로 (화면에는 표시하지 않는다)
#[tauri::command]
fn sidecar_info(state: tauri::State<SidecarState>) -> SidecarInfo {
    SidecarInfo {
        base: format!("http://127.0.0.1:{}", state.port),
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
#[tauri::command]
fn save_error_report(app: tauri::AppHandle, state: tauri::State<SidecarState>) -> Result<bool, String> {
    use tauri_plugin_dialog::DialogExt;

    let report_body = fetch_error_report(&state).unwrap_or_else(|e| {
        // sidecar가 죽어 있어도 최소 정보는 저장한다
        format!("{{\"error\":\"sidecar unavailable\",\"detail\":\"{e}\"}}")
    });
    let meta = serde_json::json!({
        "appVersion": app.package_info().version.to_string(),
        "os": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
    })
    .to_string();

    let picked = app
        .dialog()
        .file()
        .set_file_name("medbridge-error-report.zip")
        .blocking_save_file();
    let Some(path) = picked else {
        return Ok(false); // 사용자가 취소
    };
    let path = path.into_path().map_err(|e| e.to_string())?;

    let file = std::fs::File::create(&path).map_err(|e| e.to_string())?;
    let mut z = zip::ZipWriter::new(file);
    let opts: zip::write::SimpleFileOptions = Default::default();
    z.start_file("report.json", opts).map_err(|e| e.to_string())?;
    z.write_all(report_body.as_bytes()).map_err(|e| e.to_string())?;
    z.start_file("app.json", opts).map_err(|e| e.to_string())?;
    z.write_all(meta.as_bytes()).map_err(|e| e.to_string())?;
    z.finish().map_err(|e| e.to_string())?;
    Ok(true)
}

fn fetch_error_report(state: &SidecarState) -> Result<String, String> {
    let url = format!("http://127.0.0.1:{}/api/system/error-report", state.port);
    ureq::get(&url)
        .set("X-MedBridge-Token", &state.token)
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

fn spawn_sidecar(app: &tauri::AppHandle, port: u16, token: &str) -> std::io::Result<Child> {
    let app_data_dir = app.path().app_data_dir().expect("app data dir unavailable");
    std::fs::create_dir_all(&app_data_dir).ok();

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
    if let Ok(resource_dir) = app.path().resource_dir() {
        let ocr_dir = resource_dir.join("resources").join("ocr");
        if ocr_dir.is_dir() {
            cmd.env("MEDBRIDGE_OCR_DIR", &ocr_dir);
        }
    }
    cmd.env("MEDBRIDGE_APP_DATA_DIR", &app_data_dir)
        .env("MEDBRIDGE_API_TOKEN", token)
        .env("MEDBRIDGE_BOUND_PORT", port.to_string())
        .env(
            "APP_ENV",
            if cfg!(debug_assertions) { "development" } else { "production" },
        )
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
}

/// sidecar가 실제로 우리 포트에 떠서 /health에 정상 응답할 때까지 대기.
/// 다른 프로세스가 포트를 선점한 경우 응답 검증에서 걸러진다.
fn wait_for_sidecar(port: u16, child: &Mutex<Option<Child>>, timeout: Duration) -> Result<(), String> {
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
    if let Ok(mut guard) = state.child.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

pub fn run() {
    let port = pick_free_port();
    let token = uuid::Uuid::new_v4().to_string();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup({
            let token = token.clone();
            move |app| {
                let child = spawn_sidecar(&app.handle().clone(), port, &token)
                    .map_err(|e| format!("failed to start sidecar: {e}"))?;
                app.manage(SidecarState {
                    port,
                    token: token.clone(),
                    child: Mutex::new(Some(child)),
                    startup: Mutex::new(Startup::Starting),
                });
                // 창은 즉시 뜨고(준비 화면), 준비 확인은 백그라운드에서 진행한다
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    let state = handle.state::<SidecarState>();
                    let result =
                        wait_for_sidecar(port, &state.child, Duration::from_secs(120));
                    let mut startup = state.startup.lock().unwrap();
                    *startup = match result {
                        Ok(()) => Startup::Ready,
                        Err(e) => {
                            eprintln!("sidecar startup failed: {e}");
                            Startup::Failed
                        }
                    };
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
        // (강제 종료·크래시 대비 Job Object 수준 봉쇄는 후속 Windows 작업)
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
