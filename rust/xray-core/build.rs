use std::env;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-env-changed=RUSTC");
    println!("cargo:rerun-if-env-changed=RUSTUP_TOOLCHAIN");

    let rustc = env::var_os("RUSTC").expect("RUSTC must be set by cargo");
    let output = Command::new(&rustc)
        .arg("--version")
        .output()
        .unwrap_or_else(|error| panic!("failed to run build compiler {:?}: {}", rustc, error));
    if !output.status.success() {
        panic!(
            "build compiler {:?} --version failed with status {}",
            rustc, output.status
        );
    }

    let version = String::from_utf8(output.stdout)
        .expect("build compiler --version output must be valid UTF-8")
        .trim()
        .to_owned();
    if version.is_empty() {
        panic!("build compiler --version returned an empty version");
    }

    println!("cargo:rustc-env=CIDX_HOST_RUSTC_VERSION={version}");
}
