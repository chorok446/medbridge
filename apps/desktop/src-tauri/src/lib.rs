//! MedBridge 데스크톱 셸.
//!
//! 역할: 창 표시, FastAPI sidecar의 시작·준비 확인·감시·종료, 앱 데이터 경로·동적 포트·토큰 전달.
//! sidecar는 127.0.0.1의 임의 포트에 바인딩되고, 토큰 없는 요청은 거부한다.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;

struct SidecarState {
    port: u16,
    token: String,
    child: Mutex<Option<Child>>,
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

fn pick_free_port() -> u16 {
    // ponytail: OS가 고른 빈 포트를 즉시 반환 — 해제~기동 사이 짧은 선점 경쟁은
    // 아래 준비 확인(HTTP /health 검증)으로 흡수한다. 문제가 실측되면 IPC 방식으로 교체.
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
            "uvicorn",
            "app.main:app",
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

    cmd.env("MEDBRIDGE_APP_DATA_DIR", &app_data_dir)
        .env("MEDBRIDGE_API_TOKEN", token)
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
fn wait_for_sidecar(port: u16, child: &mut Child, timeout: Duration) -> Result<(), String> {
    let deadline = std::time::Instant::now() + timeout;
    let addr = format!("127.0.0.1:{port}");
    while std::time::Instant::now() < deadline {
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!("sidecar exited early: {status}"));
        }
        if let Ok(mut stream) = TcpStream::connect(&addr) {
            let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
            let req = format!(
                "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
            );
            if stream.write_all(req.as_bytes()).is_ok() {
                let mut buf = String::new();
                let _ = stream.read_to_string(&mut buf);
                if buf.contains("\"status\":\"ok\"") {
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
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup({
            let token = token.clone();
            move |app| {
                let mut child = spawn_sidecar(&app.handle().clone(), port, &token)
                    .map_err(|e| format!("failed to start sidecar: {e}"))?;
                // 준비되기 전에 GUI가 요청을 보내지 않도록 기동 시 확인한다
                // (마이그레이션 실패 등으로 sidecar가 죽으면 여기서 앱을 종료)
                if let Err(e) = wait_for_sidecar(port, &mut child, Duration::from_secs(60)) {
                    let _ = child.kill();
                    return Err(format!("sidecar not ready: {e}").into());
                }
                app.manage(SidecarState {
                    port,
                    token: token.clone(),
                    child: Mutex::new(Some(child)),
                });
                Ok(())
            }
        })
        .invoke_handler(tauri::generate_handler![sidecar_info])
        .build(tauri::generate_context!())
        .expect("error while building MedBridge");

    app.run(|app_handle, event| {
        // 창 파괴·앱 종료 어느 경로로 끝나도 sidecar를 정리한다
        // (강제 종료·크래시 대비 Job Object 수준 봉쇄는 Sprint 1.5 Windows 작업)
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
