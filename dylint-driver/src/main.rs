#![feature(rustc_private)]

use anyhow::Result;

fn main() -> Result<()> {
    env_logger::init();
    let args = std::env::args_os().collect::<Vec<_>>();
    dylint_driver::dylint_driver(&args)
}
