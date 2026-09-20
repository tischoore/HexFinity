import json
import os

import pytest

import segment_settings as ss


# ---------------------------------------------------------------------------
# default_settings / validate_settings

def test_default_settings_shape():
    data = ss.default_settings()
    assert data["schema_version"] == ss.SCHEMA_VERSION
    assert data["last_directory"] is None
    assert data["segment_path"]["types"] == {}


def test_validate_settings_rejects_non_dict_root():
    with pytest.raises(ss.SettingsError):
        ss.validate_settings([1, 2, 3])


def test_validate_settings_rejects_bad_segment_path():
    with pytest.raises(ss.SettingsError):
        ss.validate_settings({"segment_path": "nope"})


def test_validate_settings_rejects_bad_types():
    with pytest.raises(ss.SettingsError):
        ss.validate_settings({"segment_path": {"types": "nope"}})


def test_validate_settings_fills_missing_keys():
    data = ss.validate_settings({})
    assert data["schema_version"] == ss.SCHEMA_VERSION
    assert data["segment_path"]["types"] == {}


# ---------------------------------------------------------------------------
# load_settings / save_settings (round trip via tmp_path)

def test_load_settings_missing_file_returns_default(tmp_path):
    path = tmp_path / "settings.json"
    data = ss.load_settings(str(path))
    assert data == ss.default_settings()
    assert not path.exists()  # load never writes


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    data = ss.default_settings()
    ss.set_last_directory(data, "C:/segments")
    ss.add_type(data, "Bridge")

    ss.save_settings(str(path), data)
    assert path.exists()

    loaded = ss.load_settings(str(path))
    assert loaded["last_directory"] == "C:/segments"
    assert ss.has_type(loaded, "Bridge")


def test_save_settings_creates_missing_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "settings.json"
    ss.save_settings(str(path), ss.default_settings())
    assert path.exists()


def test_save_settings_leaves_no_temp_files(tmp_path):
    path = tmp_path / "settings.json"
    ss.save_settings(str(path), ss.default_settings())
    remaining = os.listdir(str(tmp_path))
    assert remaining == ["settings.json"]


def test_load_settings_malformed_json_raises(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ss.SettingsError):
        ss.load_settings(str(path))


def test_load_settings_bad_shape_raises(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"segment_path": {"types": []}}), encoding="utf-8")
    with pytest.raises(ss.SettingsError):
        ss.load_settings(str(path))


# ---------------------------------------------------------------------------
# add_type / has_type

def test_add_type_idempotent():
    data = ss.default_settings()
    ss.add_type(data, "Bridge")
    ss.add_type(data, "Bridge")
    assert list(data["segment_path"]["types"].keys()) == ["Bridge"]


def test_has_type_false_for_unknown():
    data = ss.default_settings()
    assert not ss.has_type(data, "Bridge")


# ---------------------------------------------------------------------------
# find_segment / add_segment dedup

_HULL = [(-10.0, -5.0), (10.0, -5.0), (10.0, 5.0), (-10.0, 5.0)]
_WAYPOINTS = [(-10.0, 0.0, 12.0, 3), (10.0, 0.0, 12.0, 1)]


def test_add_segment_creates_type_if_missing():
    data = ss.default_settings()
    entry = ss.add_segment(data, "Bridge", "C:/segs/Bridge/a.stl", _HULL, _WAYPOINTS,
                            is_end_segment=False, edge_snap=3)
    assert ss.has_type(data, "Bridge")
    assert entry["file"] == "C:/segs/Bridge/a.stl"
    assert entry["hull_local_mm"] == [list(p) for p in _HULL]
    assert entry["waypoints"][0] == {"x_mm": -10.0, "y_mm": 0.0, "z_mm": 12.0, "edge_idx": 3}
    assert entry["man_height_mm"] == ss.DEFAULT_MAN_HEIGHT_MM
    assert "added_utc" in entry


def test_find_segment_matches_normalized_path():
    data = ss.default_settings()
    ss.add_segment(data, "Bridge", "C:/segs/Bridge/a.stl", _HULL, _WAYPOINTS,
                    False, 3)
    found = ss.find_segment(data, "Bridge", "c:/SEGS/Bridge/A.STL")
    assert found is not None
    assert found["file"] == "C:/segs/Bridge/a.stl"


def test_find_segment_none_for_different_type():
    data = ss.default_settings()
    ss.add_segment(data, "Bridge", "C:/segs/Bridge/a.stl", _HULL, _WAYPOINTS,
                    False, 3)
    assert ss.find_segment(data, "Tunnel", "C:/segs/Bridge/a.stl") is None


def test_add_segment_raises_on_duplicate():
    data = ss.default_settings()
    ss.add_segment(data, "Bridge", "C:/segs/Bridge/a.stl", _HULL, _WAYPOINTS,
                    False, 3)
    with pytest.raises(ss.DuplicateSegmentError):
        ss.add_segment(data, "Bridge", "c:/SEGS/bridge/A.stl", _HULL, _WAYPOINTS,
                        False, 3)


def test_add_segment_same_file_different_type_allowed():
    data = ss.default_settings()
    ss.add_segment(data, "Bridge", "C:/segs/shared/a.stl", _HULL, _WAYPOINTS,
                    False, 3)
    # Not a duplicate: different type namespace.
    entry = ss.add_segment(data, "Tunnel", "C:/segs/shared/a.stl", _HULL, _WAYPOINTS,
                            False, 3)
    assert entry["file"] == "C:/segs/shared/a.stl"


def test_last_directory_getter_setter():
    data = ss.default_settings()
    assert ss.get_last_directory(data) is None
    ss.set_last_directory(data, "C:/segs")
    assert ss.get_last_directory(data) == "C:/segs"
