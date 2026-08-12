fn main() -> std::io::Result<()> {
    let _ = std::fs::read_to_string("release-fixture.txt")?;
    Ok(())
}
