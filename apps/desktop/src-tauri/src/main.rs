// Windows에서 콘솔 창 숨김
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    medbridge_lib::run();
}
