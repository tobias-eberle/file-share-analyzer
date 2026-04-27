"""Folder-path tag extraction. Messy paths intentionally."""
from __future__ import annotations

from share_analyzer.tags import (
    BLOCKLIST, MAX_TAG_LEN, MAX_TAGS, extract_tags,
)


def test_basic_windows_path_drops_drive_letter():
    tags = extract_tags(r"Z:\maschinen\12345\anleitungen\gasmesser\xyz.pdf")
    assert tags == ["maschinen", "12345", "anleitungen", "gasmesser"]


def test_basic_posix_path():
    tags = extract_tags("/srv/share/projects/alpha/notes.md")
    assert tags == ["srv", "share", "projects", "alpha"]


def test_unc_path_drops_empty_leading_segments():
    """`\\\\server\\share\\Foo\\bar.pdf` splits to ['', '', 'server',
    'share', 'Foo', 'bar.pdf']; the empties must be dropped, not
    surface as blank tags."""
    tags = extract_tags(r"\\server\share\Foo\bar.pdf")
    assert tags == ["server", "share", "foo"]


def test_long_path_prefix_stripped_for_drive():
    tags = extract_tags(r"\\?\Z:\maschinen\foo.pdf")
    # Without the strip, 'Z:' would also need to dodge the drive-letter
    # filter; the extra '?' segment would leak through if we didn't
    # peel the prefix.
    assert tags == ["maschinen"]
    assert "?" not in tags
    assert "z:" not in tags


def test_long_path_prefix_unc_stripped():
    tags = extract_tags(r"\\?\UNC\server\share\foo\bar.pdf")
    assert tags == ["server", "share", "foo"]
    assert "unc" not in tags
    assert "?" not in tags


def test_mixed_separators():
    tags = extract_tags(r"Z:\foo/bar\baz/file.pdf")
    assert tags == ["foo", "bar", "baz"]


def test_trailing_separator_does_not_lose_last_folder():
    """A trailing separator means the 'filename' is empty, but the
    last actual folder must still appear."""
    tags = extract_tags(r"Z:\maschinen\12345\\")
    assert "12345" in tags
    assert "maschinen" in tags


def test_double_separators_collapsed():
    tags = extract_tags(r"Z:\\foo\\\\bar\\\\baz\\file.pdf")
    assert tags == ["foo", "bar", "baz"]


def test_underscore_prefix_treated_as_private():
    tags = extract_tags(r"Z:\foo\_archive\bar\baz.pdf")
    assert "_archive" not in tags
    assert "archive" not in tags  # also not a stripped form
    assert tags == ["foo", "bar"]


def test_dot_prefix_hidden_treated_as_hidden():
    tags = extract_tags("/srv/share/.git/config")
    assert ".git" not in tags
    assert "git" not in tags
    assert tags == ["srv", "share"]


def test_dollar_prefix_system_treated_as_system():
    tags = extract_tags(r"Z:\$RECYCLE.BIN\S-1-5\file.tmp")
    # $RECYCLE.BIN is excluded by the `$` prefix rule; the SID-like
    # subfolder also passes the length check but is not stripped of
    # case sensitivity, so it survives. That's fine — it's not in
    # the blocklist and the rule we're testing is the `$` prefix.
    assert "$recycle.bin" not in tags
    assert "recycle.bin" not in tags


def test_blocklist_drops_organisational_chrome():
    tags = extract_tags(r"Z:\Shared\backup\final\xyz.pdf")
    # Every component is in BLOCKLIST → empty result.
    assert tags == []


def test_blocklist_does_not_drop_substrings():
    """'archive' is blocklisted; 'archives_2024' is not."""
    tags = extract_tags(r"Z:\projects\archives_2024\report.pdf")
    assert "projects" in tags
    assert "archives_2024" in tags
    assert "archives" not in tags


def test_lowercases_for_matching():
    tags = extract_tags(r"Z:\Maschinen\12345\Foo.pdf")
    assert "maschinen" in tags
    assert "Maschinen" not in tags


def test_dedupes_after_lowercase():
    """Maschinen/maschinen would emit two 'maschinen' tags otherwise."""
    tags = extract_tags(r"Z:\Maschinen\maschinen\file.pdf")
    assert tags.count("maschinen") == 1


def test_unicode_preserved():
    tags = extract_tags("/srv/share/Projets/Élise/notes.md")
    assert "élise" in tags
    assert "projets" in tags


def test_spaces_within_folder_name_preserved():
    tags = extract_tags(r"Z:\My Project\sub folder\file.pdf")
    assert "my project" in tags
    assert "sub folder" in tags


def test_special_chars_preserved_in_folder_names():
    tags = extract_tags(r"Z:\M-13206\(2024)\file.pdf")
    assert "m-13206" in tags
    assert "(2024)" in tags


def test_single_char_folder_dropped():
    tags = extract_tags("/a/b/c/d/file.txt")
    # All single-char folders drop; result is empty.
    assert tags == []


def test_folder_at_max_tag_len_kept():
    name = "x" * MAX_TAG_LEN
    tags = extract_tags(f"/share/{name}/file.pdf")
    assert name in tags


def test_folder_above_max_tag_len_dropped():
    name = "x" * (MAX_TAG_LEN + 1)
    tags = extract_tags(f"/share/{name}/file.pdf")
    assert name not in tags
    assert "share" in tags  # rest of the path still tagged


def test_max_tags_caps_pathologically_deep_paths():
    # Build a path with 50 unique folder segments.
    segments = "/".join(f"folder_{i:02d}" for i in range(50))
    tags = extract_tags(f"/{segments}/file.pdf")
    assert len(tags) == MAX_TAGS
    # The first MAX_TAGS segments survive in order.
    assert tags == [f"folder_{i:02d}" for i in range(MAX_TAGS)]


def test_empty_input_returns_empty_list():
    assert extract_tags("") == []
    assert extract_tags(None) == []  # type: ignore[arg-type]


def test_filename_only_no_separator_returns_empty():
    """`'foo.pdf'` has no folder component."""
    assert extract_tags("foo.pdf") == []


def test_whitespace_in_folder_stripped():
    tags = extract_tags(r"Z:\  foo  \  bar  \file.pdf")
    assert "foo" in tags
    assert "bar" in tags
    # No leading/trailing whitespace leaks into the tag.
    for t in tags:
        assert t == t.strip()


def test_returns_fresh_list_each_call():
    """Caller must be free to mutate the result without side effects
    on cached state."""
    a = extract_tags("/srv/share/foo/bar.pdf")
    a.append("mutated")
    b = extract_tags("/srv/share/foo/bar.pdf")
    assert "mutated" not in b


def test_blocklist_is_a_frozenset():
    """Sanity: the constant is a real frozenset, not a list — keeps
    membership tests O(1)."""
    assert isinstance(BLOCKLIST, frozenset)
    assert "shared" in BLOCKLIST


def test_realistic_german_smb_path():
    """The kind of path users actually see on industrial Z: drives —
    spaces, parens, German, mixed casing, deep."""
    p = (
        r"Z:\M13206 (Struktur der techn. Dokumentation)"
        r"\02 Elektrotechnik\Schaltpläne\Anlage 1\file.pdf"
    )
    tags = extract_tags(p)
    assert "m13206 (struktur der techn. dokumentation)" in tags
    assert "02 elektrotechnik" in tags
    assert "schaltpläne" in tags
    assert "anlage 1" in tags
