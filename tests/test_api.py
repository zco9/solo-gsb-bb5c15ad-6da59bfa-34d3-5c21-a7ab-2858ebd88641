"""API 端到端测试：使用临时 SQLite 库，通过 TestClient 走完整请求链路。"""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("IMD_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:  # 触发 lifespan -> init_db
        yield c


def make_payload(**overrides):
    payload = {
        "name": "site-a",
        "scan_start_hz": 1,
        "scan_end_hz": 10_000,
        "guard_interval_hz": 150,
        "carriers_hz": [100, 300, 400],
        "guard_bands": [],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- 站点创建与校验

def test_create_site_ok(client):
    resp = client.post("/sites", json=make_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == 1
    assert body["carriers_hz"] == [100, 300, 400]
    assert body["guard_interval_hz"] == 150
    # 查询已保存方案
    assert client.get(f"/sites/{body['id']}").json()["name"] == "site-a"
    assert len(client.get("/sites").json()) == 1


def test_duplicate_carriers_rejected_as_whole_batch(client):
    resp = client.post("/sites", json=make_payload(carriers_hz=[100, 200, 100]))
    assert resp.status_code == 422
    assert client.get("/sites").json() == []  # 整批拒绝，未写入任何数据


def test_invalid_guard_band_rejected_as_whole_batch(client):
    bad = make_payload(guard_bands=[{"start_hz": 500, "end_hz": 500}])
    assert client.post("/sites", json=bad).status_code == 422
    bad2 = make_payload(guard_bands=[{"start_hz": 900, "end_hz": 100}])
    assert client.post("/sites", json=bad2).status_code == 422
    assert client.get("/sites").json() == []


def test_invalid_scan_range_rejected(client):
    assert client.post("/sites", json=make_payload(scan_end_hz=1)).status_code == 422
    assert client.get("/sites").json() == []


def test_non_positive_values_rejected(client):
    assert client.post("/sites", json=make_payload(carriers_hz=[0, 100])).status_code == 422
    assert client.post("/sites", json=make_payload(guard_interval_hz=-1)).status_code == 422
    assert client.get("/sites").json() == []


def test_duplicate_site_name_conflict(client):
    assert client.post("/sites", json=make_payload()).status_code == 201
    resp = client.post("/sites", json=make_payload())
    assert resp.status_code == 409
    sites = client.get("/sites").json()
    assert len(sites) == 1  # 第二个方案未残留


def test_get_missing_site_404(client):
    assert client.get("/sites/999").status_code == 404


# ---------------------------------------------------------------- 互调分析

def test_analysis_aggregates_same_frequency_sources(client):
    # 载频 [100, 300, 400]：2*300-100=500 与 2*400-300=500 同频，须聚合
    client.post("/sites", json=make_payload())
    resp = client.post("/sites/1/analysis")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    freqs = [p["freq_hz"] for p in body["products"]]
    assert freqs == sorted(freqs)  # 按频率稳定排序
    assert freqs == [200, 500, 700]  # 范围外产物（<=0）已被过滤

    p500 = next(p for p in body["products"] if p["freq_hz"] == 500)
    assert {tuple(s.values()) for s in p500["sources"]} == {(300, 100), (400, 300)}

    # 命中对象与最短距离
    p200 = next(p for p in body["products"] if p["freq_hz"] == 200)
    assert p200["distance_hz"] == 100
    assert p200["hit"] == {"type": "carrier", "carrier_hz": 100, "guard_band": None}
    assert p200["violates_guard_interval"] is True  # 100 < 150

    p700 = next(p for p in body["products"] if p["freq_hz"] == 700)
    assert p700["hit"]["carrier_hz"] == 400
    assert p700["distance_hz"] == 300
    assert p700["violates_guard_interval"] is False  # 300 >= 150


def test_analysis_hits_guard_band_with_zero_distance(client):
    payload = make_payload(
        carriers_hz=[1000, 1100],
        guard_bands=[{"start_hz": 880, "end_hz": 920}],
        guard_interval_hz=50,
    )
    client.post("/sites", json=payload)
    body = client.post("/sites/1/analysis").json()

    products = {p["freq_hz"]: p for p in body["products"]}
    assert set(products) == {900, 1200}

    # 900 落在保护频段 [880, 920] 内：距离 0，命中频段
    assert products[900]["distance_hz"] == 0
    assert products[900]["hit"]["type"] == "guard_band"
    assert products[900]["hit"]["guard_band"] == {"start_hz": 880, "end_hz": 920}
    assert products[900]["violates_guard_interval"] is True

    # 1200 距载频 1100 为 100，距频段最近边界 280 -> 命中载频
    assert products[1200]["hit"] == {"type": "carrier", "carrier_hz": 1100, "guard_band": None}
    assert products[1200]["distance_hz"] == 100


def test_analysis_filters_out_of_scan_range(client):
    payload = make_payload(carriers_hz=[200, 500], scan_start_hz=500, scan_end_hz=900)
    client.post("/sites", json=payload)
    body = client.post("/sites/1/analysis").json()
    # 2*200-500=-100 与 2*500-200=800：仅 800 在 [500, 900] 内
    assert [p["freq_hz"] for p in body["products"]] == [800]
    assert body["product_count"] == 1


def test_analysis_persisted_and_queryable(client):
    client.post("/sites", json=make_payload())
    created = client.post("/sites/1/analysis").json()

    latest = client.get("/sites/1/analysis").json()
    assert latest == created  # 查询已保存的分析结果

    by_id = client.get(f"/analyses/{created['analysis_id']}").json()
    assert by_id == created

    metas = client.get("/sites/1/analyses").json()
    assert [m["analysis_id"] for m in metas] == [created["analysis_id"]]
    assert metas[0]["product_count"] == created["product_count"]


def test_analysis_404s(client):
    assert client.post("/sites/999/analysis").status_code == 404
    assert client.get("/sites/999/analysis").status_code == 404
    assert client.get("/analyses/999").status_code == 404
    client.post("/sites", json=make_payload())
    assert client.get("/sites/1/analysis").status_code == 404  # 尚未分析


# ---------------------------------------------------------------- 幂等键（Idempotency-Key）

def test_analysis_without_idempotency_key_keeps_existing_behavior(client):
    # 未提供请求头：每次 POST 都执行并保存一份新分析（201）
    client.post("/sites", json=make_payload())
    first = client.post("/sites/1/analysis")
    second = client.post("/sites/1/analysis")
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["analysis_id"] > first.json()["analysis_id"]
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == [
        first.json()["analysis_id"],
        second.json()["analysis_id"],
    ]


def test_idempotency_key_replays_first_result_without_new_record(client):
    client.post("/sites", json=make_payload())
    headers = {"Idempotency-Key": "run-001"}
    first = client.post("/sites/1/analysis", headers=headers)
    assert first.status_code == 201, first.text

    replay = client.post("/sites/1/analysis", headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()  # 完全返回首次结果（含 analysis_id/created_at）

    # 再次重放仍为同一份结果，且只新增过一条分析记录
    replay2 = client.post("/sites/1/analysis", headers=headers)
    assert replay2.status_code == 200
    assert replay2.json() == first.json()
    assert client.get("/sites/1/analyses").json() == [
        {
            "analysis_id": first.json()["analysis_id"],
            "site_id": 1,
            "created_at": first.json()["created_at"],
            "product_count": first.json()["product_count"],
        }
    ]
    # 最近分析与按 id 查询都指向首次结果；现有查询接口保持兼容
    assert client.get("/sites/1/analysis").json() == first.json()
    assert client.get(f"/analyses/{first.json()['analysis_id']}").json() == first.json()
    # 重放也可经风险分组等下游接口读到同一份持久化结果
    assert client.get(
        f"/analyses/{first.json()['analysis_id']}/risk-groups"
    ).status_code == 200


def test_idempotency_keys_are_scoped_per_site(client):
    client.post("/sites", json=make_payload())
    client.post("/sites", json=make_payload(name="site-b"))
    # 不同站点使用相同键互不影响，各自首次执行
    r1 = client.post("/sites/1/analysis", headers={"Idempotency-Key": "shared-key"})
    r2 = client.post("/sites/2/analysis", headers={"Idempotency-Key": "shared-key"})
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["site_id"] == 1 and r2.json()["site_id"] == 2
    assert r1.json()["analysis_id"] != r2.json()["analysis_id"]
    # 各自重放仍命中本站点的首次结果
    assert client.post(
        "/sites/1/analysis", headers={"Idempotency-Key": "shared-key"}
    ).json()["analysis_id"] == r1.json()["analysis_id"]
    assert client.post(
        "/sites/2/analysis", headers={"Idempotency-Key": "shared-key"}
    ).json()["analysis_id"] == r2.json()["analysis_id"]


def test_different_idempotency_keys_produce_separate_analyses(client):
    client.post("/sites", json=make_payload())
    r1 = client.post("/sites/1/analysis", headers={"Idempotency-Key": "key-one"})
    r2 = client.post("/sites/1/analysis", headers={"Idempotency-Key": "key-two"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.json()["analysis_id"] > r1.json()["analysis_id"]
    assert len(client.get("/sites/1/analyses").json()) == 2
    # 不带键的请求不影响已登记键，反之亦然
    r3 = client.post("/sites/1/analysis")
    assert r3.status_code == 201
    replay = client.post("/sites/1/analysis", headers={"Idempotency-Key": "key-one"})
    assert replay.status_code == 200
    assert replay.json()["analysis_id"] == r1.json()["analysis_id"]


def test_blank_idempotency_key_rejected_as_422(client):
    client.post("/sites", json=make_payload())
    for blank in ("", " ", "   ", "\t", " \t "):
        resp = client.post("/sites/1/analysis", headers={"Idempotency-Key": blank})
        assert resp.status_code == 422, repr(blank)
        assert "Idempotency-Key" in resp.json()["detail"]
    # 空白键被拒绝时不登记、不产生任何分析记录
    assert client.get("/sites/1/analyses").json() == []
    # 非空白键含空白字符仍视为合法（不做 strip）
    ok = client.post("/sites/1/analysis", headers={"Idempotency-Key": " key "})
    assert ok.status_code == 201


def test_idempotency_key_on_missing_site_still_404(client):
    assert client.post(
        "/sites/999/analysis", headers={"Idempotency-Key": "k"}
    ).status_code == 404


def test_failed_first_request_rolls_back_registration(client, monkeypatch):
    # 首次请求在事务内计算失败时登记行与分析都回滚；随后同键请求应能作为
    # 新的首次请求重新执行成功，而不是永久卡死或重放到不存在的结果
    client.post("/sites", json=make_payload())
    from app import main as main_module
    from app import analysis as analysis_module

    def boom(*args, **kwargs):
        raise RuntimeError("boom during analysis")

    with monkeypatch.context() as patch:
        patch.setattr(main_module, "analyze", boom)
        with pytest.raises(Exception, match="boom"):
            client.post("/sites/1/analysis", headers={"Idempotency-Key": "retry-me"})
    # 离开 context 后路由模块引用已恢复（IMD_DB_PATH 等其他补丁不受影响）
    assert main_module.analyze is analysis_module.analyze

    retry = client.post("/sites/1/analysis", headers={"Idempotency-Key": "retry-me"})
    assert retry.status_code == 201, retry.text
    replay = client.post("/sites/1/analysis", headers={"Idempotency-Key": "retry-me"})
    assert replay.status_code == 200
    assert replay.json() == retry.json()
    assert len(client.get("/sites/1/analyses").json()) == 1


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_concurrent_requests_with_same_idempotency_key_produce_one_result(
    tmp_path, monkeypatch
):
    # 真实 HTTP 并发重放：in-process uvicorn + 多线程。分析计算被刻意放慢，
    # 保证多个请求在首个事务内重叠到达；BEGIN IMMEDIATE + UNIQUE(site_id,key)
    # 必须使它们只产生一份分析结果。
    db_path = tmp_path / "conc.db"
    monkeypatch.setenv("IMD_DB_PATH", str(db_path))

    port = _free_tcp_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as http:
            r = http.post(
                "/sites",
                json=make_payload(),
            )
            assert r.status_code == 201, r.text

            from app import analysis as analysis_module
            from app import main as main_module

            real_analyze = main_module.analyze

            def slow_analyze(*args, **kwargs):
                time.sleep(0.5)  # 放大窗口，确保并发请求在同一时刻重叠
                return real_analyze(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(main_module, "analyze", slow_analyze)
                n = 8
                barrier = threading.Barrier(n)
                responses: list[httpx.Response | None] = [None] * n

                def worker(i: int) -> None:
                    barrier.wait()
                    responses[i] = http.post(
                        "/sites/1/analysis",
                        headers={"Idempotency-Key": "concurrent-key"},
                    )

                threads = [
                    threading.Thread(target=worker, args=(i,)) for i in range(n)
                ]
                start = time.monotonic()
                for th in threads:
                    th.start()
                for th in threads:
                    th.join()
                elapsed = time.monotonic() - start

            statuses = sorted(resp.status_code for resp in responses if resp is not None)
            assert statuses == [200] * (n - 1) + [201], statuses
            bodies = [resp.json() for resp in responses if resp is not None]
            assert all(body == bodies[0] for body in bodies)
            # 全部并发请求合计只产生一份分析记录
            metas = http.get("/sites/1/analyses").json()
            assert len(metas) == 1
            assert metas[0]["analysis_id"] == bodies[0]["analysis_id"]
            # 串行化生效：总耗时远小于 n 次完整分析（0.5s）串行之和
            assert elapsed < n * 0.5

            # 并发结束后再用同键请求，仍是稳定的 200 重放
            after = http.post(
                "/sites/1/analysis", headers={"Idempotency-Key": "concurrent-key"}
            )
            assert after.status_code == 200
            assert after.json() == bodies[0]

            # 分析模块恢复后，不带键的请求仍产生新分析
            extra = http.post("/sites/1/analysis")
            assert extra.status_code == 201
            assert len(http.get("/sites/1/analyses").json()) == 2
            assert analysis_module.analyze is main_module.analyze
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


# ---------------------------------------------------------------- 风险分组

def test_risk_groups_404(client):
    assert client.get("/analyses/999/risk-groups").status_code == 404


def test_risk_groups_empty_when_no_violation(client):
    # guard_interval_hz=0：distance < 0 永不成立，无违规产物 -> 空列表
    client.post("/sites", json=make_payload(guard_interval_hz=0))
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    resp = client.get(f"/analyses/{analysis_id}/risk-groups")
    assert resp.status_code == 200
    assert resp.json() == []


def test_risk_groups_merge_multi_source_and_shared_carriers(client):
    # 载频 [100, 300, 400]：违规产物 200（来源 (300,400)）与 500（来源 (300,100)、
    # (400,300)，同频已合并）共享载频 300/400，归为一组；非违规的 700 不参与
    client.post("/sites", json=make_payload())
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    resp = client.get(f"/analyses/{analysis_id}/risk-groups")
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "group_id": 1,
            "carriers_hz": [100, 300, 400],
            "product_freqs_hz": [200, 500],
            "source_pair_count": 3,  # 200 有 1 个来源，500 有 2 个来源
            "distance_hz": 100,  # min(100, 100)
        }
    ]


def test_risk_groups_split_disjoint_components(client):
    # 两簇载频各产生违规产物；非违规产物 9900（来源含载频 100 与 5000）
    # 不参与分组，不得把两簇桥接成一组
    payload = make_payload(
        carriers_hz=[100, 200, 5000, 6000],
        scan_start_hz=1,
        scan_end_hz=100000,
        guard_interval_hz=1100,
    )
    client.post("/sites", json=payload)
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    groups = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    assert groups == [
        {
            "group_id": 1,
            "carriers_hz": [100, 200],
            "product_freqs_hz": [300],
            "source_pair_count": 1,
            "distance_hz": 100,
        },
        {
            "group_id": 2,
            "carriers_hz": [5000, 6000],
            "product_freqs_hz": [4000, 7000],
            "source_pair_count": 2,
            "distance_hz": 1000,
        },
    ]


def test_risk_groups_transitive_carrier_sharing(client):
    # 300 的载频 {100,200} 与 1200 的 {400,800} 不直接共享，
    # 仅经 600/700 链式共享载频传递归为一组
    payload = make_payload(
        carriers_hz=[100, 200, 400, 800],
        scan_start_hz=1,
        scan_end_hz=2000,
        guard_interval_hz=450,
    )
    client.post("/sites", json=payload)
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    groups = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    assert groups == [
        {
            "group_id": 1,
            "carriers_hz": [100, 200, 400, 800],
            "product_freqs_hz": [300, 600, 700, 1200],
            "source_pair_count": 4,
            "distance_hz": 100,
        }
    ]


def test_risk_groups_use_persisted_result(client):
    # 分组基于已持久化的分析结果：同一 analysis_id 重复查询结果一致
    client.post("/sites", json=make_payload())
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    first = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    second = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    assert first == second
    assert [g["group_id"] for g in first] == list(range(1, len(first) + 1))


# ---------------------------------------------------------------- 新增载频禁用区间

def test_carrier_exclusion_ranges_both_formulas_and_merge(client):
    # 载频 [1000, 5000]，g=10：违规产物位置为 (990,1010) 与 (4990,5010) 的整数点。
    # 公式一 2*x-c：x∈[996,1004]（c=1000，ceil(1991/2)/floor(2009/2) 检验除 2 取整）、
    # [2996,3004]（两个载频各贡献一次）、[4996,5004]（c=5000）；
    # 公式二 2*c-x：x∈[991,1009]、[4991,5009]、[8991,9009]（负区间被候选范围裁掉）；
    # 载频点 [1000,1000]、[5000,5000] 被相邻区间吸收合并。
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 5000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    resp = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 100000},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "candidate_start_hz": 1,
        "candidate_end_hz": 100000,
        "exclusion_ranges": [
            {"start_hz": 991, "end_hz": 1009},
            {"start_hz": 2996, "end_hz": 3004},
            {"start_hz": 4991, "end_hz": 5009},
            {"start_hz": 8991, "end_hz": 9009},
        ],
    }


def test_carrier_exclusion_ranges_open_boundary(client):
    # 开边界：产物到载频的距离恰好等于 g 时不违规。单载频 1000、g=10 时
    # 禁用区间为 [991,1009]；若误用闭边界会得到 [990,1010]
    client.post("/sites", json=make_payload(
        carriers_hz=[1000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 100000},
    ).json()
    assert body["exclusion_ranges"] == [{"start_hz": 991, "end_hz": 1009}]


def test_carrier_exclusion_ranges_with_guard_band(client):
    # 保护频段 [480,520]、g=150 产生违规产物位置 [331,669]，载频 1000 产生
    # [851,1149]；反解后载频点 [1000,1000] 被 [851,1149] 吸收
    client.post("/sites", json=make_payload(
        carriers_hz=[1000],
        guard_bands=[{"start_hz": 480, "end_hz": 520}],
        guard_interval_hz=150,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 10000},
    ).json()
    assert body["exclusion_ranges"] == [
        {"start_hz": 666, "end_hz": 834},
        {"start_hz": 851, "end_hz": 1149},
        {"start_hz": 1331, "end_hz": 1669},
    ]


def test_carrier_exclusion_ranges_clipped_to_candidate(client):
    # 与上一组相同的数据，候选范围 [1005, 4990]：首区间被裁剪，
    # [4991,5009] 与 [8991,9009] 整体落在候选范围外被丢弃
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 5000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1005, "candidate_end_hz": 4990},
    ).json()
    assert body["exclusion_ranges"] == [
        {"start_hz": 1005, "end_hz": 1009},
        {"start_hz": 2996, "end_hz": 3004},
    ]


def test_carrier_exclusion_ranges_zero_guard_interval(client):
    # g=0：任何产物都不违规，仅现有载频点禁用；相邻载频点 [100,100] 与
    # [101,101] 合并为 [100,101]
    client.post("/sites", json=make_payload(
        carriers_hz=[100, 101, 105], guard_interval_hz=0,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 1000},
    ).json()
    assert body["exclusion_ranges"] == [
        {"start_hz": 100, "end_hz": 101},
        {"start_hz": 105, "end_hz": 105},
    ]


def test_carrier_exclusion_ranges_empty_when_nothing_forbidden(client):
    # 候选范围避开全部禁用位置时返回空列表
    client.post("/sites", json=make_payload(
        carriers_hz=[1000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 2000, "candidate_end_hz": 3000},
    ).json()
    assert body["exclusion_ranges"] == []


def test_carrier_exclusion_ranges_404(client):
    resp = client.get(
        "/sites/999/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 100},
    )
    assert resp.status_code == 404


def test_carrier_exclusion_ranges_param_validation(client):
    client.post("/sites", json=make_payload())
    base = "/sites/1/carrier-exclusion-ranges"
    # 缺参 / 非正整数 / 非整数 -> 422
    assert client.get(base).status_code == 422
    assert client.get(base, params={"candidate_start_hz": 1}).status_code == 422
    for bad in (
        {"candidate_start_hz": 0, "candidate_end_hz": 100},
        {"candidate_start_hz": 1, "candidate_end_hz": -5},
        {"candidate_start_hz": "abc", "candidate_end_hz": 100},
        {"candidate_start_hz": 1.5, "candidate_end_hz": 100},
        {"candidate_start_hz": 200, "candidate_end_hz": 100},  # 起点大于终点
    ):
        resp = client.get(base, params=bad)
        assert resp.status_code == 422, bad
    # 起点等于终点合法（闭区间退化为单点候选范围）
    ok = client.get(base, params={"candidate_start_hz": 300, "candidate_end_hz": 300})
    assert ok.status_code == 200
    assert ok.json()["exclusion_ranges"] == [{"start_hz": 300, "end_hz": 300}]


def test_carrier_exclusion_ranges_does_not_save_analysis(client):
    # 该端点不执行也不保存分析：调用后仍无分析记录
    client.post("/sites", json=make_payload())
    resp = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 10000},
    )
    assert resp.status_code == 200
    assert client.get("/sites/1/analysis").status_code == 404
    assert client.get("/sites/1/analyses").json() == []




def _create_and_analyze(client, **payload_overrides):
    client.post("/sites", json=make_payload(**payload_overrides))
    return client.post("/sites/1/analysis").json()["analysis_id"]


def test_mitigation_plan_404(client):
    assert client.get("/analyses/999/mitigation-plan", params={"max_disabled": 3}).status_code == 404


def test_mitigation_plan_max_disabled_validation(client):
    analysis_id = _create_and_analyze(client)
    # 缺少必填参数 / 越界 / 非整数 -> 422
    assert client.get(f"/analyses/{analysis_id}/mitigation-plan").status_code == 422
    for bad in (0, 9, -1):
        resp = client.get(
            f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": bad}
        )
        assert resp.status_code == 422, bad
    assert (
        client.get(
            f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": "x"}
        ).status_code
        == 422
    )


def test_mitigation_plan_empty_when_no_violation(client):
    # guard_interval_hz=0：无违规产物，无需停用任何载频
    analysis_id = _create_and_analyze(client, guard_interval_hz=0)
    resp = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "analysis_id": analysis_id,
        "max_disabled": 1,
        "feasible": True,
        "disabled_carriers_hz": [],
        "eliminated_product_freqs_hz": [],
        "remaining_product_freqs_hz": [],
    }


def test_mitigation_plan_single_carrier_solution(client):
    # 载频 [1000, 1100]：违规产物 900 只有来源 (1000,1100)，停用其一即可，
    # 多个最小解时按字典序停用较小载频 1000
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[1000, 1100],
        guard_bands=[{"start_hz": 880, "end_hz": 920}],
        guard_interval_hz=50,
    )
    resp = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "analysis_id": analysis_id,
        "max_disabled": 1,
        "feasible": True,
        "disabled_carriers_hz": [1000],
        "eliminated_product_freqs_hz": [900],
        "remaining_product_freqs_hz": [],
    }


def test_mitigation_plan_lexicographic_tie_break(client):
    # 违规边 {100,200}/{200,400}/{400,100}/{400,800}：最小点覆盖大小为 2，
    # 多个最小解（{100,400}、{200,400}）中按升序列表字典序取 [100, 400]；
    # 上限 1 时无解，尽力方案停用 400 可消除 3 个产物、仅剩 300
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[100, 200, 400, 800],
        scan_end_hz=2000,
        guard_interval_hz=450,
    )
    infeasible = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    ).json()
    assert infeasible["feasible"] is False
    assert infeasible["disabled_carriers_hz"] == [400]
    assert infeasible["eliminated_product_freqs_hz"] == [600, 700, 1200]
    assert infeasible["remaining_product_freqs_hz"] == [300]

    feasible = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert feasible == {
        "analysis_id": analysis_id,
        "max_disabled": 2,
        "feasible": True,
        "disabled_carriers_hz": [100, 400],
        "eliminated_product_freqs_hz": [300, 600, 700, 1200],
        "remaining_product_freqs_hz": [],
    }


def test_mitigation_plan_all_sources_must_be_covered(client):
    # 产物 700 有两个互不共享载频的来源 (400,100) 与 (500,300)：只停一个载频
    # 不能消除它（必须两个来源组合各停用至少一个载频）
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[100, 300, 400, 500],
        scan_start_hz=695,
        scan_end_hz=710,
        guard_bands=[{"start_hz": 698, "end_hz": 702}],
        guard_interval_hz=10,
    )
    one = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    ).json()
    assert one["feasible"] is False
    # 只停一个载频最多覆盖一个来源组合，700 仍未消除；字典序最小的尽力选择是 100
    assert one["disabled_carriers_hz"] == [100]
    assert one["eliminated_product_freqs_hz"] == []
    assert one["remaining_product_freqs_hz"] == [700]

    two = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert two["feasible"] is True
    # 两条来源边 {100,400}、{300,500} 各停一个端点，字典序最小为 [100, 300]
    assert two["disabled_carriers_hz"] == [100, 300]
    assert two["eliminated_product_freqs_hz"] == [700]
    assert two["remaining_product_freqs_hz"] == []


def test_mitigation_plan_three_disjoint_edges(client):
    # 三对互不相连的载频各有一个定点违规产物（小保护间隔 + 窄保护频段，
    # 不规则间隔避免跨对产物恰好落在其他载频上而产生连边）：需恰好停用 3 个
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[1000, 1150, 4700, 4900, 12300, 12600],
        scan_start_hz=1,
        scan_end_hz=40000,
        guard_bands=[
            {"start_hz": 845, "end_hz": 855},
            {"start_hz": 4495, "end_hz": 4505},
            {"start_hz": 11995, "end_hz": 12005},
        ],
        guard_interval_hz=10,
    )
    two = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert two["feasible"] is False
    # 上限 2 只能消除两对中的产物；消除数并列时按字典序取较小两对的小端点
    assert two["disabled_carriers_hz"] == [1000, 4700]
    assert two["eliminated_product_freqs_hz"] == [850, 4500]
    assert two["remaining_product_freqs_hz"] == [12000]

    three = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 3}
    ).json()
    assert three["feasible"] is True
    assert three["disabled_carriers_hz"] == [1000, 4700, 12300]
    assert three["eliminated_product_freqs_hz"] == [850, 4500, 12000]
    assert three["remaining_product_freqs_hz"] == []

    # 上限 8 同样返回基数最小（3）的解，不多停用载频
    eight = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 8}
    ).json()
    assert eight["feasible"] is True
    assert eight["disabled_carriers_hz"] == [1000, 4700, 12300]


def test_mitigation_plan_uses_persisted_result(client):
    # 仅使用已保存结果：同一 analysis_id 重复查询（含不同上限）结果确定可复现
    analysis_id = _create_and_analyze(client)
    first = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    second = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert first == second


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- 跨站点冲突检测

def _make_site(client, site_id, name=None, **overrides):
    """按默认参数创建站点（id 从 1 起连续，故直接返回传入 site_id）。"""
    payload = make_payload(name=name or f"site-{site_id}", **overrides)
    assert client.post("/sites", json=payload).status_code == 201, payload
    return site_id


def _analyze(client, site_id):
    return client.post(f"/sites/{site_id}/analysis").json()["analysis_id"]


def test_cross_site_conflicts_basic_pairing_and_clusters(client):
    # 站点 1 产物 {200, 500, 700}（距离 100/100/300）；
    # 站点 2 产物 {500, 800, 900, 1400, 1800, 2000}（距离 500/200/100/100/300/500）。
    # tolerance=10 时跨站仅 500~500 配对 -> 单簇
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[1000, 1200, 1500], scan_end_hz=10_000)
    a1 = _analyze(client, 1)
    a2 = _analyze(client, 2)

    resp = client.post(
        "/cross-site-conflicts", json={"site_ids": [2, 1], "tolerance_hz": 10}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_ids": [2, 1],  # 回显保持请求顺序
        "tolerance_hz": 10,
        "analyses": [
            {"site_id": 2, "analysis_id": a2},
            {"site_id": 1, "analysis_id": a1},
        ],
        "cluster_count": 1,
        "clusters": [
            {
                "cluster_id": 1,
                "site_ids": [1, 2],
                "products": [
                    {"site_id": 1, "freq_hz": 500, "distance_hz": 100},
                    {"site_id": 2, "freq_hz": 500, "distance_hz": 500},
                ],
                "min_distance_hz": 100,
                "pair_count": 1,
                "pairs": [
                    {
                        "site_a_id": 1,
                        "product_a_freq_hz": 500,
                        "site_b_id": 2,
                        "product_b_freq_hz": 500,
                        "frequency_delta_hz": 0,
                    }
                ],
            }
        ],
    }


def test_cross_site_conflicts_transitive_chaining(client):
    # 站点 1: 990/1020；站点 2: 1000/1030；站点 3: 1010/1040，tolerance=10。
    # 990-1000-1010-1020 沿跨站边链式相连（站点 1 的 990/1020 同站不直连，
    # 但二者经站点 2、站点 3 的跨站配对桥接），6 个节点合为一簇；
    # 站点 4(4990/5020)、站点 5(5000/5030) 各自独立成两簇。
    _make_site(client, 1, carriers_hz=[1000, 1010], scan_start_hz=900, scan_end_hz=1100)
    _make_site(client, 2, carriers_hz=[1010, 1020], scan_start_hz=900, scan_end_hz=1100)
    _make_site(client, 3, carriers_hz=[1020, 1030], scan_start_hz=900, scan_end_hz=1100)
    _make_site(client, 4, carriers_hz=[5000, 5010], scan_start_hz=900, scan_end_hz=6000)
    _make_site(client, 5, carriers_hz=[5010, 5020], scan_start_hz=900, scan_end_hz=6000)
    for site_id in range(1, 6):
        _analyze(client, site_id)

    resp = client.post(
        "/cross-site-conflicts", json={"site_ids": [5, 1, 4, 3, 2], "tolerance_hz": 10}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # analyses 与回显 site_ids 同序
    assert [a["site_id"] for a in body["analyses"]] == [5, 1, 4, 3, 2]
    assert [a["analysis_id"] for a in body["analyses"]] == [5, 1, 4, 3, 2]
    assert body["cluster_count"] == 3

    first = body["clusters"][0]
    assert first["cluster_id"] == 1
    assert first["site_ids"] == [1, 2, 3]
    assert [(p["site_id"], p["freq_hz"]) for p in first["products"]] == [
        (1, 990), (1, 1020), (2, 1000), (2, 1030), (3, 1010), (3, 1040)
    ]
    assert first["min_distance_hz"] == 10
    assert first["pair_count"] == 5
    assert first["pairs"] == [
        {"site_a_id": 1, "product_a_freq_hz": 990, "site_b_id": 2,
         "product_b_freq_hz": 1000, "frequency_delta_hz": 10},
        {"site_a_id": 1, "product_a_freq_hz": 1020, "site_b_id": 2,
         "product_b_freq_hz": 1030, "frequency_delta_hz": 10},
        {"site_a_id": 1, "product_a_freq_hz": 1020, "site_b_id": 3,
         "product_b_freq_hz": 1010, "frequency_delta_hz": 10},
        {"site_a_id": 2, "product_a_freq_hz": 1000, "site_b_id": 3,
         "product_b_freq_hz": 1010, "frequency_delta_hz": 10},
        {"site_a_id": 2, "product_a_freq_hz": 1030, "site_b_id": 3,
         "product_b_freq_hz": 1040, "frequency_delta_hz": 10},
    ]

    # 后两簇仅涉及站点 4、5；同站的 4990/5020（差 30）不直连，不合并
    assert [cl["site_ids"] for cl in body["clusters"][1:]] == [[4, 5], [4, 5]]
    assert [
        sorted(p["freq_hz"] for p in cl["products"]) for cl in body["clusters"][1:]
    ] == [[4990, 5000], [5020, 5030]]
    assert [cl["cluster_id"] for cl in body["clusters"]] == [1, 2, 3]


def test_cross_site_conflicts_within_site_products_do_not_bridge(client):
    # 站点 2 的同站产物 906/910（差 4 <= tolerance）不直接连边：
    # 900~906 与 910~914 本可借同站近频桥接成一簇，实际必须保持两簇。
    _make_site(client, 1, carriers_hz=[896, 892], scan_start_hz=899, scan_end_hz=901)
    _make_site(client, 2, carriers_hz=[900, 894, 890], scan_start_hz=905, scan_end_hz=911)
    _make_site(client, 3, carriers_hz=[912, 910], scan_start_hz=913, scan_end_hz=915)
    for site_id in (1, 2, 3):
        _analyze(client, site_id)

    body = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2, 3], "tolerance_hz": 6}
    ).json()
    assert body["cluster_count"] == 2
    assert body["clusters"][0] == {
        "cluster_id": 1,
        "site_ids": [1, 2],
        "products": [
            {"site_id": 1, "freq_hz": 900, "distance_hz": 4},
            {"site_id": 2, "freq_hz": 906, "distance_hz": 6},
        ],
        "min_distance_hz": 4,
        "pair_count": 1,
        "pairs": [
            {"site_a_id": 1, "product_a_freq_hz": 900, "site_b_id": 2,
             "product_b_freq_hz": 906, "frequency_delta_hz": 6}
        ],
    }
    assert body["clusters"][1]["site_ids"] == [2, 3]
    assert body["clusters"][1]["min_distance_hz"] == 2  # min(10, 2)


def test_cross_site_conflicts_empty_when_no_nearby_products(client):
    # 站点 1 产物 {200,500,700}；站点 2 产物 {2900,3200}：无频率差 <= 1000 的配对
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[3000, 3100], scan_end_hz=10_000)
    for site_id in (1, 2):
        _analyze(client, site_id)

    body = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 1000}
    ).json()
    assert body["cluster_count"] == 0
    assert body["clusters"] == []
    assert [a["site_id"] for a in body["analyses"]] == [1, 2]


def test_cross_site_conflicts_zero_tolerance_means_exact_match_only(client):
    # tolerance_hz=0：仅同频 500~500 配对；700 与 500（差 200）不配对
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[1000, 1500], scan_end_hz=10_000)
    for site_id in (1, 2):
        _analyze(client, site_id)

    body = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 0}
    ).json()
    assert body["cluster_count"] == 1
    assert body["clusters"][0]["pairs"] == [
        {"site_a_id": 1, "product_a_freq_hz": 500, "site_b_id": 2,
         "product_b_freq_hz": 500, "frequency_delta_hz": 0}
    ]


def test_cross_site_conflicts_uses_latest_analysis(client):
    # 读取各站点最近一次分析：重复分析后 analysis_id 应取最新一条
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[1000, 1200, 1500], scan_end_hz=10_000)
    first_1 = _analyze(client, 1)
    _analyze(client, 2)
    latest_1 = _analyze(client, 1)  # 站点 1 的第二次分析
    latest_2 = _analyze(client, 2)
    assert latest_1 > first_1

    body = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 10}
    ).json()
    assert body["analyses"] == [
        {"site_id": 1, "analysis_id": latest_1},
        {"site_id": 2, "analysis_id": latest_2},
    ]
    # 同一请求重复调用结果确定可复现
    again = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 10}
    ).json()
    assert again["clusters"] == body["clusters"]


def test_cross_site_conflicts_404_missing_site(client):
    _make_site(client, 1)
    _analyze(client, 1)
    # 任一站点不存在 -> 404（即使其他站点合法）
    resp = client.post("/cross-site-conflicts", json={"site_ids": [1, 999], "tolerance_hz": 10})
    assert resp.status_code == 404
    assert "999" in resp.json()["detail"]


def test_cross_site_conflicts_404_site_without_analysis(client):
    _make_site(client, 1)
    _make_site(client, 2)
    _analyze(client, 1)  # 仅站点 1 有分析
    resp = client.post("/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 10})
    assert resp.status_code == 404
    assert "2" in resp.json()["detail"]


def test_cross_site_conflicts_422_validation(client):
    _make_site(client, 1)
    _analyze(client, 1)

    def post(body):
        return client.post("/cross-site-conflicts", json=body)

    # 站点数少于 2 / 多于 8
    assert post({"site_ids": [1], "tolerance_hz": 10}).status_code == 422
    assert post({"site_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9], "tolerance_hz": 10}).status_code == 422
    # 站点 ID 重复 / 非正
    assert post({"site_ids": [1, 1], "tolerance_hz": 10}).status_code == 422
    assert post({"site_ids": [1, 0], "tolerance_hz": 10}).status_code == 422
    assert post({"site_ids": [1, -2], "tolerance_hz": 10}).status_code == 422
    # tolerance_hz 缺失 / 为负 / 非整数
    assert post({"site_ids": [1, 2]}).status_code == 422
    assert post({"site_ids": [1, 2], "tolerance_hz": -1}).status_code == 422
    assert post({"site_ids": [1, 2], "tolerance_hz": 1.5}).status_code == 422
    assert post({"site_ids": [1, 2], "tolerance_hz": "x"}).status_code == 422
    # 缺整个请求体
    assert client.post("/cross-site-conflicts").status_code == 422
    # 参数非法时不查询站点：重复 ID 即使站点不存在也先由校验拒绝
    assert post({"site_ids": [998, 998], "tolerance_hz": 10}).status_code == 422


def test_cross_site_conflicts_does_not_write_analysis(client):
    # 检测只读取已保存分析，不产生新的分析记录
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[1000, 1500], scan_end_hz=10_000)
    a1 = _analyze(client, 1)
    a2 = _analyze(client, 2)
    client.post("/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 10})
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == [a1]
    assert [m["analysis_id"] for m in client.get("/sites/2/analyses").json()] == [a2]


def test_cross_site_conflicts_eight_sites_limit(client):
    # 8 个站点为允许上限：站点间产物互不相近 -> 200 空簇响应
    for i in range(1, 9):
        _make_site(
            client, i,
            carriers_hz=[10_000 * i, 10_000 * i + 100],
            scan_start_hz=1,
            scan_end_hz=100_000,
        )
        _analyze(client, i)
    resp = client.post(
        "/cross-site-conflicts",
        json={"site_ids": list(range(1, 9)), "tolerance_hz": 10},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cluster_count"] == 0
    assert len(resp.json()["analyses"]) == 8


# ---------------------------------------------------------------- 载频漂移风险评估

def _drift_body(tolerances):
    """tolerances: (carrier_hz, tolerance_hz) 列表 -> 请求体（保留重复条目）。"""
    return {
        "tolerances": [
            {"carrier_hz": carrier, "tolerance_hz": tolerance}
            for carrier, tolerance in tolerances
        ]
    }


def test_drift_risk_merges_shared_drift_variables(client):
    # 载频 [1000, 1300]，容差 {1000: 100, 1300: 50}，g=200。
    # 来源 (1000,1300)：标称 700，可达区间 [450, 950]；与载频 1000（c==fa）
    # 比较时 δ_a 系数合并为 2-1=1，半径 100+50=150，最小距离
    # max(0, 300-150)=150（若误把产物区间与载频区间当独立区间，半径 350
    # 会错得 0）；与载频 1300（c==fb）半径 2*100+2*50=300，距离 300。
    # 来源 (1300,1000)：标称 1600，可达 [1400, 1800]，最小距离 150。
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 1300], guard_interval_hz=200, scan_end_hz=100000,
    ))
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(1000, 100), (1300, 50)])
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "guard_interval_hz": 200,
        "risk_count": 2,
        "risks": [
            {
                "source": {"fa_hz": 1000, "fb_hz": 1300},
                "nominal_freq_hz": 700,
                "reachable_start_hz": 450,
                "reachable_end_hz": 950,
                "min_distance_hz": 150,
                "hits": [
                    {"type": "carrier", "carrier_hz": 1000, "guard_band": None}
                ],
            },
            {
                "source": {"fa_hz": 1300, "fb_hz": 1000},
                "nominal_freq_hz": 1600,
                "reachable_start_hz": 1400,
                "reachable_end_hz": 1800,
                "min_distance_hz": 150,
                "hits": [
                    {"type": "carrier", "carrier_hz": 1300, "guard_band": None}
                ],
            },
        ],
    }


def test_drift_risk_reports_all_tied_hits_and_ignores_scan_range(client):
    # 零容差退化为静态距离。载频 [400, 600, 1000]，g=250，扫描范围仅
    # [1, 300]：全频域评估不按扫描范围过滤，标称 800 的来源仍参与。
    # (600,400)：标称 800，到载频 600 与 1000 的距离同为 200 -> 并列命中；
    # (400,600) 与 (600,1000) 标称同为 200，按 (标称频率, fa, fb) 排序。
    client.post("/sites", json=make_payload(
        carriers_hz=[400, 600, 1000], scan_end_hz=300, guard_interval_hz=250,
    ))
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(400, 0), (600, 0), (1000, 0)])
    )
    assert resp.status_code == 200, resp.text
    risks = resp.json()["risks"]
    assert [
        (r["nominal_freq_hz"], r["source"]["fa_hz"], r["source"]["fb_hz"])
        for r in risks
    ] == [(200, 400, 600), (200, 600, 1000), (800, 600, 400)]
    # 零容差时可达区间退化为标称频率单点
    assert risks[0]["reachable_start_hz"] == risks[0]["reachable_end_hz"] == 200
    assert risks[0]["min_distance_hz"] == 200
    assert risks[0]["hits"] == [
        {"type": "carrier", "carrier_hz": 400, "guard_band": None}
    ]
    # 并列命中：载频 600 与 1000 距离同为 200，按载频升序列出
    assert risks[2]["min_distance_hz"] == 200
    assert risks[2]["hits"] == [
        {"type": "carrier", "carrier_hz": 600, "guard_band": None},
        {"type": "carrier", "carrier_hz": 1000, "guard_band": None},
    ]


def test_drift_risk_tied_hits_include_guard_band(client):
    # 零容差，载频 [1000, 1400]，保护频段 [100, 200]，g=500。
    # 来源 (1000,1400)：标称 600，到载频 1000 与保护频段 [100,200] 的距离
    # 同为 400 -> 并列命中（载频在前，保护频段在后）
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 1400],
        guard_bands=[{"start_hz": 100, "end_hz": 200}],
        guard_interval_hz=500,
        scan_end_hz=100000,
    ))
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(1000, 0), (1400, 0)])
    )
    assert resp.status_code == 200, resp.text
    risks = resp.json()["risks"]
    assert [r["nominal_freq_hz"] for r in risks] == [600, 1800]
    assert risks[0]["min_distance_hz"] == 400
    assert risks[0]["hits"] == [
        {"type": "carrier", "carrier_hz": 1000, "guard_band": None},
        {
            "type": "guard_band",
            "carrier_hz": None,
            "guard_band": {"start_hz": 100, "end_hz": 200},
        },
    ]
    assert risks[1]["hits"] == [
        {"type": "carrier", "carrier_hz": 1400, "guard_band": None}
    ]


def test_drift_risk_guard_band_interval_distance(client):
    # 载频 [1000, 1300]，容差 {1000: 60, 1300: 0}，保护频段 [750, 900]，
    # g=250。来源 (1000,1300)：标称 700，可达 [580, 820]，与频段重叠 ->
    # 区间距离 0。来源 (1300,1000)：标称 1600，可达 [1540, 1660]，到频段
    # 距离 640、到载频 1300（c==fa，半径 60）距离 240 -> 最小 240。
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 1300],
        guard_bands=[{"start_hz": 750, "end_hz": 900}],
        guard_interval_hz=250,
        scan_end_hz=100000,
    ))
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(1000, 60), (1300, 0)])
    )
    assert resp.status_code == 200, resp.text
    risks = resp.json()["risks"]
    assert [r["nominal_freq_hz"] for r in risks] == [700, 1600]
    assert risks[0]["min_distance_hz"] == 0
    assert risks[0]["hits"] == [
        {
            "type": "guard_band",
            "carrier_hz": None,
            "guard_band": {"start_hz": 750, "end_hz": 900},
        }
    ]
    assert risks[1]["min_distance_hz"] == 240
    assert risks[1]["hits"] == [
        {"type": "carrier", "carrier_hz": 1300, "guard_band": None}
    ]


def test_drift_risk_strict_guard_interval_threshold(client):
    # 严格小于：最小可能距离恰好等于 guard_interval_hz 的来源不返回。
    # 零容差，载频 [1000, 1400]：两个来源的最小距离都是 400。
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 1400], guard_interval_hz=400, scan_end_hz=100000,
    ))
    body = _drift_body([(1000, 0), (1400, 0)])
    resp = client.post("/sites/1/drift-risk", json=body)
    assert resp.status_code == 200
    assert resp.json()["risks"] == []
    assert resp.json()["risk_count"] == 0
    # 保护间隔放宽到 401 时两个来源都进入结果
    client.post("/sites", json=make_payload(
        name="site-b",
        carriers_hz=[1000, 1400],
        guard_interval_hz=401,
        scan_end_hz=100000,
    ))
    assert client.post("/sites/2/drift-risk", json=body).json()["risk_count"] == 2


def test_drift_risk_full_response_example(client):
    # README 示例：载频 [100, 300, 400]，保护频段 [480, 520]，g=150，
    # 容差 {100: 10, 300: 20, 400: 30}
    client.post("/sites", json=make_payload(
        guard_bands=[{"start_hz": 480, "end_hz": 520}],
    ))
    resp = client.post(
        "/sites/1/drift-risk",
        json=_drift_body([(100, 10), (300, 20), (400, 30)]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "guard_interval_hz": 150,
        "risk_count": 4,
        "risks": [
            {
                "source": {"fa_hz": 300, "fb_hz": 400},
                "nominal_freq_hz": 200,
                "reachable_start_hz": 130,
                "reachable_end_hz": 270,
                "min_distance_hz": 20,
                "hits": [
                    {"type": "carrier", "carrier_hz": 100, "guard_band": None}
                ],
            },
            {
                "source": {"fa_hz": 300, "fb_hz": 100},
                "nominal_freq_hz": 500,
                "reachable_start_hz": 450,
                "reachable_end_hz": 550,
                "min_distance_hz": 0,
                "hits": [
                    {
                        "type": "guard_band",
                        "carrier_hz": None,
                        "guard_band": {"start_hz": 480, "end_hz": 520},
                    }
                ],
            },
            {
                "source": {"fa_hz": 400, "fb_hz": 300},
                "nominal_freq_hz": 500,
                "reachable_start_hz": 420,
                "reachable_end_hz": 580,
                "min_distance_hz": 0,
                "hits": [
                    {
                        "type": "guard_band",
                        "carrier_hz": None,
                        "guard_band": {"start_hz": 480, "end_hz": 520},
                    }
                ],
            },
            {
                "source": {"fa_hz": 400, "fb_hz": 100},
                "nominal_freq_hz": 700,
                "reachable_start_hz": 630,
                "reachable_end_hz": 770,
                "min_distance_hz": 110,
                "hits": [
                    {
                        "type": "guard_band",
                        "carrier_hz": None,
                        "guard_band": {"start_hz": 480, "end_hz": 520},
                    }
                ],
            },
        ],
    }


def test_drift_risk_404(client):
    resp = client.post("/sites/999/drift-risk", json=_drift_body([(100, 0)]))
    assert resp.status_code == 404


def test_drift_risk_mapping_validation(client):
    client.post("/sites", json=make_payload())  # 载频 [100, 300, 400]
    url = "/sites/1/drift-risk"
    # 边界值 0 与 1000000 合法
    ok = _drift_body([(100, 0), (300, 10), (400, 1_000_000)])
    assert client.post(url, json=ok).status_code == 200
    # 缺失载频 400 / 多余载频 500 / 重复载频 100
    assert client.post(url, json=_drift_body([(100, 0), (300, 0)])).status_code == 422
    assert (
        client.post(
            url, json=_drift_body([(100, 0), (300, 0), (400, 0), (500, 0)])
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, json=_drift_body([(100, 0), (100, 1), (300, 0), (400, 0)])
        ).status_code
        == 422
    )
    # 容差越界 / 非整数 / 缺失
    assert (
        client.post(url, json=_drift_body([(100, -1), (300, 0), (400, 0)])).status_code
        == 422
    )
    assert (
        client.post(
            url, json=_drift_body([(100, 1_000_001), (300, 0), (400, 0)])
        ).status_code
        == 422
    )
    assert (
        client.post(url, json=_drift_body([(100, 1.5), (300, 0), (400, 0)])).status_code
        == 422
    )
    assert (
        client.post(
            url, json=_drift_body([(100, "x"), (300, 0), (400, 0)])
        ).status_code
        == 422
    )
    assert (
        client.post(
            url,
            json={
                "tolerances": [
                    {"carrier_hz": 100},
                    {"carrier_hz": 300, "tolerance_hz": 0},
                    {"carrier_hz": 400, "tolerance_hz": 0},
                ]
            },
        ).status_code
        == 422
    )
    # 空映射 / 缺请求体
    assert client.post(url, json={"tolerances": []}).status_code == 422
    assert client.post(url).status_code == 422


def test_drift_risk_does_not_save_analysis(client):
    # 该端点不执行也不保存分析：调用后仍无分析记录
    client.post("/sites", json=make_payload())
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(100, 10), (300, 20), (400, 30)])
    )
    assert resp.status_code == 200
    assert client.get("/sites/1/analysis").status_code == 404
    assert client.get("/sites/1/analyses").json() == []


def test_drift_risk_single_carrier_site_has_no_sources(client):
    # 单载频站点没有有序来源组合：合法映射下返回空风险列表
    client.post("/sites", json=make_payload(carriers_hz=[1000]))
    resp = client.post("/sites/1/drift-risk", json=_drift_body([(1000, 100)]))
    assert resp.status_code == 200
    assert resp.json()["risk_count"] == 0
    assert resp.json()["risks"] == []
