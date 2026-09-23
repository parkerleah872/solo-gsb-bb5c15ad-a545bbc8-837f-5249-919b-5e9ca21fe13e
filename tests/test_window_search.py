"""跨设备窗口检索 GET /windows/search 测试。

覆盖：筛选（时间范围闭区间 / open|sealed / 设备标签）、(window_start, device_id) 排序、
绑定筛选条件的稳定 keyset 游标分页（新增数据不重复不遗漏）、非法/过期/条件不符游标 400、
非法参数 422，以及响应结构。
"""


def ingest(client, device_id, events):
    return client.post("/ingest", json={"device_id": device_id, "events": events})


def put_labels(client, device_id, labels):
    return client.put(f"/devices/{device_id}/labels", json={"labels": labels})


def search(client, **params):
    return client.get("/windows/search", params=params)


def paginate_all(client, page_size, **params):
    """按游标翻完全部页，返回 (keys, pages) ；keys 为 (window_start, device_id)。"""
    keys: list[tuple[str, str]] = []
    pages = 0
    cursor = None
    while True:
        query = {"limit": page_size, **params}
        if cursor is not None:
            query["cursor"] = cursor
        body = search(client, **query).json()
        pages += 1
        for item in body["items"]:
            keys.append((item["window_start"], item["device_id"]))
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 1000  # 防御：游标必须收敛
    return keys, pages


# ---------- 基本检索与排序 ----------

def test_empty_search_returns_empty_page(client):
    r = search(client)
    assert r.status_code == 200
    body = r.json()
    assert body == {"items": [], "next_cursor": None}


def test_results_ordered_by_window_start_then_device_id(client):
    ingest(client, "zdev", [{"event_id": "e1", "event_time": "2026-09-22T00:00:10Z", "value": 1.0}])
    ingest(client, "adev", [{"event_id": "e1", "event_time": "2026-09-22T00:00:20Z", "value": 2.0}])
    ingest(client, "mdev", [{"event_id": "e1", "event_time": "2026-09-22T00:02:05Z", "value": 3.0}])

    body = search(client).json()
    keys = [(i["window_start"], i["device_id"]) for i in body["items"]]
    # 同一 window_start 下按 device_id 升序（adev < zdev），其后才是 00:02 的窗口
    assert keys == [
        ("2026-09-22T00:00:00Z", "adev"),
        ("2026-09-22T00:00:00Z", "zdev"),
        ("2026-09-22T00:02:00Z", "mdev"),
    ]
    # 元素即窗口聚合 + 设备标识，结构与单设备窗口接口一致
    first = body["items"][0]
    assert first["device_id"] == "adev"
    assert first["window_end"] == "2026-09-22T00:01:00Z"
    assert (first["count"], first["sum"], first["avg"]) == (1, 2.0, 2.0)
    assert first["status"] == "open"
    assert body["next_cursor"] is None


def test_window_start_range_is_inclusive(client):
    for t in ("2026-09-22T00:00:10Z", "2026-09-22T00:01:10Z", "2026-09-22T00:02:10Z"):
        ingest(client, "d1", [{"event_id": t, "event_time": t, "value": 1.0}])

    body = search(
        client,
        window_start_from="2026-09-22T00:01:00Z",
        window_start_to="2026-09-22T00:02:00Z",
    ).json()
    assert [i["window_start"] for i in body["items"]] == [
        "2026-09-22T00:01:00Z",
        "2026-09-22T00:02:00Z",
    ]


def test_status_filter_open_and_sealed(client):
    ingest(client, "d1", [
        {"event_id": "e1", "event_time": "2026-09-22T00:00:10Z", "value": 1.0},
        {"event_id": "e2", "event_time": "2026-09-22T00:02:00Z", "value": 2.0},
    ])
    sealed = search(client, status="sealed").json()["items"]
    opens = search(client, status="open").json()["items"]
    assert [i["window_start"] for i in sealed] == ["2026-09-22T00:00:00Z"]
    assert all(i["status"] == "sealed" for i in sealed)
    assert [i["window_start"] for i in opens] == ["2026-09-22T00:02:00Z"]
    assert all(i["status"] == "open" for i in opens)


# ---------- 标签筛选 ----------

def test_label_filter_key_only_and_key_value(client):
    ingest(client, "d-hall", [{"event_id": "e1", "event_time": "2026-09-22T00:00:10Z", "value": 1.0}])
    ingest(client, "d-lobby", [{"event_id": "e1", "event_time": "2026-09-22T00:00:10Z", "value": 1.0}])
    put_labels(client, "d-hall", [{"key": "Room", "value": "Hall-A"}])
    put_labels(client, "d-lobby", [{"key": "room", "value": "Lobby"}])

    by_key = search(client, label_key=" ROOM ").json()["items"]  # 键名同写入侧规范化
    assert {i["device_id"] for i in by_key} == {"d-hall", "d-lobby"}

    by_kv = search(client, label_key="room", label_value=" Hall-A ").json()["items"]
    assert {i["device_id"] for i in by_kv} == {"d-hall"}


def test_label_value_without_key_is_422(client):
    r = search(client, label_value="Hall-A")
    assert r.status_code == 422


def test_from_later_than_to_is_422(client):
    r = search(
        client,
        window_start_from="2026-09-22T00:02:00Z",
        window_start_to="2026-09-22T00:00:00Z",
    )
    assert r.status_code == 422


def test_limit_bounds_are_validated(client):
    assert search(client, limit=0).status_code == 422
    assert search(client, limit=501).status_code == 422


# ---------- 游标分页 ----------

def _seed_windows(client, n_devices=3, n_starts=4):
    for di in range(n_devices):
        for si in range(n_starts):
            device = f"dev-{di}"
            t = f"2026-09-22T00:{si:02d}:10Z"
            ingest(client, device, [{"event_id": f"e-{si}", "event_time": t, "value": 1.0}])


def test_keyset_pagination_covers_all_without_duplication(client):
    _seed_windows(client)
    expected = [
        (f"2026-09-22T00:{si:02d}:00Z", f"dev-{di}")
        for si in range(4)
        for di in range(3)
    ]

    keys, pages = paginate_all(client, page_size=2)
    assert keys == expected
    assert pages == 6  # 12 条 / 每页 2 条
    assert len(keys) == len(set(keys))


def test_last_page_has_null_next_cursor_and_exact_page_size(client):
    _seed_windows(client)
    r = search(client, limit=5)
    body = r.json()
    assert len(body["items"]) == 5
    assert body["next_cursor"] is not None

    keys, _ = paginate_all(client, page_size=5)
    assert len(keys) == 12


def test_new_rows_inserted_during_pagination_do_not_duplicate_or_gap(client):
    _seed_windows(client)

    # 第一页
    first = search(client, limit=4).json()
    seen = {(i["window_start"], i["device_id"]) for i in first["items"]}
    cursor = first["next_cursor"]
    assert cursor is not None

    # 翻页进行中写入更新窗口（位于已有排序键之后），不应把已读条目挤回/挤掉
    ingest(client, "dev-9", [{"event_id": "e1", "event_time": "2026-09-22T01:00:10Z", "value": 9.0}])

    while cursor is not None:
        body = search(client, limit=4, cursor=cursor).json()
        for item in body["items"]:
            key = (item["window_start"], item["device_id"])
            assert key not in seen  # 不重复
            seen.add(key)
        cursor = body["next_cursor"]

    # 既有 12 条一条不漏；更晚的新增窗口在翻页中同样能被取到
    assert ("2026-09-22T01:00:00Z", "dev-9") in seen
    assert len(seen) == 13


def test_new_rows_in_upcoming_range_are_picked_up_without_gap(client):
    # dev-1 先有两个窗口，首页（limit=1）游标停在 (00:00, dev-1)
    ingest(client, "dev-1", [
        {"event_id": "e1", "event_time": "2026-09-22T00:00:10Z", "value": 1.0},
        {"event_id": "e2", "event_time": "2026-09-22T00:01:10Z", "value": 1.0},
    ])
    cursor = search(client, limit=1).json()["next_cursor"]
    assert cursor is not None

    # 游标之后为同一 window_start 新增另一设备的窗口（device_id 排序键在 dev-1 之后）
    ingest(client, "dev-2", [{"event_id": "e1", "event_time": "2026-09-22T00:00:20Z", "value": 1.0}])

    body = search(client, limit=10, cursor=cursor).json()
    # 不重复返回首页的 dev-1@00:00；新插入且排序键位于游标之后的 dev-2 被正常取到
    assert [(i["window_start"], i["device_id"]) for i in body["items"]] == [
        ("2026-09-22T00:00:00Z", "dev-2"),
        ("2026-09-22T00:01:00Z", "dev-1"),
    ]


def test_cursor_reused_with_different_filters_is_rejected(client):
    _seed_windows(client)
    cursor = search(client, status="sealed", limit=2).json()["next_cursor"]
    assert cursor is not None
    # 游标绑定了 status=sealed；换成 open 必须拒绝
    r = search(client, status="open", limit=2, cursor=cursor)
    assert r.status_code == 400

    # 绑定了标签筛选的游标不能用于无筛选请求
    put_labels(client, "dev-0", [{"key": "k", "value": "v"}])
    labelled_cursor = search(client, label_key="k", limit=1).json()["next_cursor"]
    assert search(client, limit=1, cursor=labelled_cursor).status_code == 400


def test_garbage_and_tampered_cursors_are_rejected(client):
    _seed_windows(client)
    assert search(client, cursor="not-a-cursor").status_code == 400
    assert search(client, cursor="a.b").status_code == 400

    cursor = search(client, limit=2).json()["next_cursor"]
    body_b64, sig_b64 = cursor.split(".", 1)
    tampered = body_b64[:-1] + ("A" if body_b64[-1] != "A" else "B") + "." + sig_b64
    assert search(client, cursor=tampered).status_code == 400
    # 改签名同样被拒
    assert search(client, cursor=body_b64 + "." + sig_b64[:-1]).status_code == 400


def test_expired_cursor_is_rejected(client, monkeypatch):
    _seed_windows(client)
    cursor = search(client, limit=2).json()["next_cursor"]
    # 把当前时间拨到游标签发 1 小时后，超出默认 300s 有效期
    import app.main as main_mod

    real_time = main_mod.time.time()
    monkeypatch.setattr(main_mod.time, "time", lambda: real_time + 3600)
    r = search(client, limit=2, cursor=cursor)
    assert r.status_code == 400


def test_cursor_expiry_ttl_configurable(client, monkeypatch):
    monkeypatch.setenv("TELEMETRY_WINDOWS_SEARCH_CURSOR_TTL", "1")
    _seed_windows(client)
    cursor = search(client, limit=2).json()["next_cursor"]
    import app.main as main_mod

    real_time = main_mod.time.time()
    monkeypatch.setattr(main_mod.time, "time", lambda: real_time + 2)
    assert search(client, limit=2, cursor=cursor).status_code == 400
