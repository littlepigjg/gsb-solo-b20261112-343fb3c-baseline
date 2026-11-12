"""处理与删除并发时的引用一致性测试。

覆盖三种交错：
1. 处理先取得使用权：删除等待副本发布，随后按后代保护规则拒绝（409）。
2. 删除先成功：处理在使用权声明处明确失败（404），不发布悬空副本。
3. 最后写入前的父级复核：父素材缺失时 save() 拒绝发布并清理新文件。

并回归多代副本溯源、无后代素材的合法删除与批量任务保护。
"""
import threading
import pytest
from backend.errors import MissingRecord, Conflict


STEPS = [{"operation": "color_invert", "params": {}}]


def _assets_and_processing(app):
    services = app.extensions["studio"]
    return services["assets"], services["processing"]


def test_claim_blocks_delete_until_copy_published(app, uploaded):
    """处理持有使用权期间，删除必须等到副本可见，再按后代规则拒绝。"""
    assets, processing = _assets_and_processing(app)
    source_id = uploaded["id"]

    with assets.usage_claim(source_id):
        entered = threading.Event()
        error_box = {}

        def delete_during_claim():
            entered.set()
            try:
                assets.delete(source_id)
            except Exception as error:  # 等待结束后按后代规则拒绝，属预期
                error_box["error"] = error

        worker = threading.Thread(target=delete_during_claim)
        worker.start()
        assert entered.wait(2)
        # 使用权未释放，删除线程必须保持阻塞（等待条件变量）。
        worker.join(timeout=0.5)
        assert worker.is_alive()
        assert assets.get(source_id)["id"] == source_id
        assert assets._usage[source_id] == 1
        worker.join(timeout=2)
        assert worker.is_alive()  # 使用权未释放，删除仍在等待
        image, record = assets.load(source_id)
        result = assets.save(image, name="受保护副本", parent=record, steps=STEPS)
    worker.join(timeout=2)
    assert not worker.is_alive()
    # 副本已发布，删除按既有后代保护规则拒绝。
    assert isinstance(error_box.get("error"), Conflict)
    with pytest.raises(Conflict):
        assets.delete(source_id)
    assert assets.get(source_id)["id"] == source_id
    assert assets.get(result["id"])["parent_id"] == source_id
    assert assets._usage == {}


def test_delete_wins_processing_fails_without_dangling_copy(app, uploaded):
    """删除先成功时，处理在声明使用权处明确失败且不产生任何副本。"""
    assets, processing = _assets_and_processing(app)
    source_id = uploaded["id"]

    assets.delete(source_id)
    before = {item["id"] for item in assets.store.all("assets")}

    with pytest.raises(MissingRecord):
        processing.process(source_id, STEPS)

    after = {item["id"] for item in assets.store.all("assets")}
    assert before == after  # 没有新副本发布
    assert not list(assets.settings.media.glob("*.tmp"))
    assert assets._usage == {}


def test_http_delete_first_then_process_returns_404(client, app, uploaded):
    """HTTP 链路：先删除成功，再处理返回业务错误，诊断保持健康。"""
    assets, _ = _assets_and_processing(app)
    source_id = uploaded["id"]
    deleted = client.delete(f"/api/assets/{source_id}")
    assert deleted.status_code == 200
    response = client.post("/api/process", json={"asset_id": source_id, "steps": STEPS})
    assert response.status_code == 404
    assert response.json["ok"] is False
    assert response.json["error"]["code"] == "not_found"
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["healthy"] is True
    assert assets._usage == {}


def test_save_rejects_when_parent_gone_before_final_write(app, uploaded):
    """绕过使用权直接构造“最后写入前父级被删”，save 必须拒绝并清理文件。"""
    assets, _ = _assets_and_processing(app)
    source_id = uploaded["id"]
    image, parent = assets.load(source_id)
    media_before = {path.name for path in assets.settings.media.glob("*.png")}

    assets.delete(source_id)  # 无副本，合法删除

    with pytest.raises(MissingRecord):
        assets.save(image, name="不应发布的副本", parent=parent, steps=STEPS)

    media_after = {path.name for path in assets.settings.media.glob("*.png")}
    assert media_after == media_before - {source_id + ".png", source_id + ".thumb.png"}
    assert not list(assets.settings.media.glob("*.tmp"))
    assert all(item.get("parent_id") != source_id for item in assets.store.all("assets"))


def test_http_process_first_then_delete_conflicts(client, app, uploaded, monkeypatch):
    """端到端：处理进行中删除会等待，副本发布后返回 409，历史完整。"""
    assets, processing = _assets_and_processing(app)
    source_id = uploaded["id"]
    inside_apply = threading.Event()
    release_apply = threading.Event()
    original_apply = processing.apply_steps

    def blocked_apply(image, steps, progress=None, cancelled=None):
        inside_apply.set()
        assert release_apply.wait(5)
        return original_apply(image, steps, progress, cancelled)

    monkeypatch.setattr(processing, "apply_steps", blocked_apply)

    result_box = {}

    def run_process():
        result_box["result"] = processing.process(source_id, STEPS)

    worker = threading.Thread(target=run_process)
    worker.start()
    assert inside_apply.wait(5)
    assert assets._usage.get(source_id) == 1

    response_box = {}

    def run_delete():
        response_box["response"] = client.delete(f"/api/assets/{source_id}")

    deleter = threading.Thread(target=run_delete)
    deleter.start()
    worker.join(timeout=2)
    assert worker.is_alive()  # 删除在等待使用权释放，处理仍在运行

    release_apply.set()
    worker.join(timeout=5)
    deleter.join(timeout=5)
    assert not worker.is_alive()
    assert not deleter.is_alive()
    response = response_box["response"]
    assert response.status_code == 409
    assert response.json["ok"] is False
    assert response.json["error"]["code"] == "conflict"

    result = result_box["result"]
    assert result["parent_id"] == source_id
    assert result["root_id"] == source_id
    lineage = client.get(f"/api/assets/{result['id']}/history").json["data"]
    assert [item["id"] for item in lineage] == [source_id, result["id"]]
    assert client.get(f"/api/assets/{source_id}").status_code == 200
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["healthy"] is True
    assert assets._usage == {}


def test_multi_generation_lineage_and_leaf_delete(client, app, uploaded):
    """多代副本溯源保留，叶节点可合法删除，父级在有后代时受保护。"""
    assets, processing = _assets_and_processing(app)
    first = processing.process(uploaded["id"], STEPS)
    second = processing.process(first["id"], STEPS)
    third = processing.process(second["id"], STEPS)

    assert first["parent_id"] == uploaded["id"]
    assert second["parent_id"] == first["id"]
    assert third["parent_id"] == second["id"]
    assert third["root_id"] == uploaded["id"]

    # 祖先与中间节点都因有后代而不能删除。
    with pytest.raises(Conflict):
        assets.delete(uploaded["id"])
    with pytest.raises(Conflict):
        assets.delete(first["id"])
    with pytest.raises(Conflict):
        assets.delete(second["id"])

    # 叶节点无后代，合法删除；逐层向上删除保持引用一致。
    assets.delete(third["id"])
    assets.delete(second["id"])
    assets.delete(first["id"])
    assets.delete(uploaded["id"])
    assert assets.store.all("assets") == []
    assert not list(assets.settings.media.glob("*.png"))
    assert assets._usage == {}
    assert client.get("/api/diagnostics").json["data"]["healthy"] is True


def test_batch_job_holds_protection_against_delete(client, app, uploaded, monkeypatch):
    """后台批量任务处理期间，删除被使用权与任务规则双重拦截。"""
    assets, processing = _assets_and_processing(app)
    source_id = uploaded["id"]
    inside_apply = threading.Event()
    release_apply = threading.Event()
    original_apply = processing.apply_steps

    def blocked_apply(image, steps, progress=None, cancelled=None):
        inside_apply.set()
        assert release_apply.wait(5)
        return original_apply(image, steps, progress, cancelled)

    monkeypatch.setattr(processing, "apply_steps", blocked_apply)

    response = client.post("/api/jobs", json={"asset_ids": [source_id], "steps": STEPS})
    assert response.status_code == 202
    assert inside_apply.wait(5)

    response_box = {}

    def run_delete():
        response_box["response"] = client.delete(f"/api/assets/{source_id}")

    deleter = threading.Thread(target=run_delete)
    deleter.start()
    release_apply.set()
    deleter.join(timeout=10)
    assert not deleter.is_alive()
    delete_response = response_box["response"]
    assert delete_response.status_code == 409
    assert delete_response.json["error"]["code"] == "conflict"

    assert assets.get(source_id)["id"] == source_id
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["healthy"] is True
    assert assets._usage == {}
