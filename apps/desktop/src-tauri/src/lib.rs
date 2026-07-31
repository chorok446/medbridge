//! MedBridge 데스크톱 셸.
//!
//! 역할: 창 표시, FastAPI sidecar의 시작·감시·종료, 앱 데이터 경로·동적 포트·토큰 전달.
//! sidecar는 127.0.0.1의 임의 포트에 바인딩되고, 토큰 없는 요청은 거부한다.

use std::net::TcpListener;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

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
    // OS가 비어 있는 포트를 고른다 — 고정 포트 하드코딩 금지
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .expect("no free port available")
}

fn spawn_sidecar(app: &tauri::AppHandle, port: u16, token: &str) -> std::io::Result<Child> {
    let app_data_dir = app
        .path()
        .app_data_dir()
        .expect("app data dir unavailable");
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
        // 패키징 모드: 번들된 sidecar 실행 파일 (리소스 디렉터리의 externalBin)
        let sidecar = app
            .path()
            .resource_dir()
            .expect("resource dir unavailable")
            .join(sidecar_binary_name());
        let mut c = Command::new(sidecar);
        c.args(["--host", "127.0.0.1", "--port", &port.to_string()]);
        c
    };

    cmd.env("MEDBRIDGE_APP_DATA_DIR", &app_data_dir)
        .env("MEDBRIDGE_API_TOKEN", token)
        .env("APP_ENV", if cfg!(debug_assertions) { "development" } else { "production" })
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
}

fn sidecar_binary_name() -> &'static str {
    if cfg!(windows) {
        "medbridge-sidecar-x86_64-pc-windows-msvc.exe"
    } else {
        "medbridge-sidecar"
    }
}

pub fn run() {
    let port = pick_free_port();
    let token = uuid::Uuid::new_v4().to_string();

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup({
            let token = token.clone();
            move |app| {
                let child = spawn_sidecar(&app.handle().clone(), port, &token)
                    .expect("failed to start sidecar");
                app.manage(SidecarState {
                    port,
                    token: token.clone(),
                    child: Mutex::new(Some(child)),
                });
                Ok(())
            }
        })
        .invoke_handler(tauri::generate_handler![sidecar_info])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                // 창 종료 시 sidecar 정상 종료
                if let Some(state) = window.app_handle().try_state::<SidecarState>() {
                    if let Ok(mut guard) = state.child.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running MedBridge");
}
