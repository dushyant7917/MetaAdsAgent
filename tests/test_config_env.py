from adhook.config import load_env_file


def test_env_file_parsing(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\n\nA_KEY=abc  # inline note\nB_KEY=\nC_KEY='quoted value'\nD_KEY=has#hash\nEXISTING=new\n")
    monkeypatch.setenv("EXISTING", "real")
    for k in ("A_KEY", "B_KEY", "C_KEY", "D_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert load_env_file(str(f)) == 3
    import os
    assert os.environ["A_KEY"] == "abc"
    assert "B_KEY" not in os.environ                  # empty value is not a key
    assert os.environ["C_KEY"] == "quoted value"
    assert os.environ["D_KEY"] == "has#hash"          # '#' without leading space is part of the value
    assert os.environ["EXISTING"] == "real"           # real environment wins
    for k in ("A_KEY", "C_KEY", "D_KEY"):
        monkeypatch.delenv(k)


def test_example_file_has_no_values():
    from adhook.config import PROJECT_ROOT
    for line in (PROJECT_ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            assert line.partition("=")[2] == "", f"{line!r} must not contain a value"
