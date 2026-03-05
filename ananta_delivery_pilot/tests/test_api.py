"""Phase 2: Merchant Pilot API conformance tests.

12 tests covering contract §8 conformance requirements:
  1.  Health returns correct fields
  2.  Work items list returns data with data_source=PILOT and canonical trio
  3.  Single work item detail returns entity with available_actions
  4.  Apply transition success — receipt with §2.2 fields + applied=true + deduped=false
  5.  Apply transition dedup — same key+payload → deduped=true, no truth writes
  6.  Apply transition conflict — same key+different payload → 409
  7.  Apply transition validation error — bad payload → 422
  8.  Evidence upload — POST /evidence with file → receipt + evidence_ref block
  9.  Event trace — GET /events/{entity_id} → ordered events
  10. Auth required — requests without X-API-Key → 401
  11. No settlement fields — none of §1.2 forbidden fields appear in any response
  12. Entity not found — apply action on nonexistent entity → 404
"""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Settlement fence: §1.2 forbidden fields
# ---------------------------------------------------------------------------
FORBIDDEN_FIELDS = {
    "release_ok",
    "hold_reason",
    "settlement_status",
    "capital_release_at",
    "waterfall_",
    "payment_release_",
    "netting_",
    "ledger_posting_",
}


def _no_forbidden(data, path="") -> list[str]:
    """Recursively check that no forbidden field names appear in a JSON structure."""
    violations = []
    if isinstance(data, dict):
        for key, val in data.items():
            for forbidden in FORBIDDEN_FIELDS:
                if key == forbidden or key.startswith(forbidden):
                    violations.append(f"{path}.{key}")
            violations.extend(_no_forbidden(val, f"{path}.{key}"))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            violations.extend(_no_forbidden(item, f"{path}[{i}]"))
    return violations


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _make_app(tmp_dir: Path):
    """Create an app instance backed by a temp DB."""
    import os
    config_dir = Path(__file__).resolve().parents[1] / "config"
    db_path = tmp_dir / ".state" / "drep.sqlite"
    evidence_dir = tmp_dir / ".state" / "evidence"

    os.environ["ANANTA_DB_PATH"] = str(db_path)
    os.environ["ANANTA_EVIDENCE_DIR"] = str(evidence_dir)
    os.environ["ANANTA_CONFIG_DIR"] = str(config_dir)
    os.environ["ANANTA_API_KEY"] = "test-key"

    # Re-import config to pick up env vars
    import importlib
    import api.config as api_config
    importlib.reload(api_config)
    import api.auth as api_auth
    importlib.reload(api_auth)

    # Init DB
    from adapters.sqlite_repo import SQLiteRepo
    from core.config import RuntimeConfig

    tmp_dir_root = tmp_dir
    shutil.copytree(config_dir, tmp_dir / "config", dirs_exist_ok=True)
    (tmp_dir / ".state").mkdir(parents=True, exist_ok=True)

    cfg = RuntimeConfig.load(tmp_dir)
    repo = SQLiteRepo(db_path)
    repo.init_db(cfg)

    # Import fresh app (override settings)
    api_config.settings.db_path = str(db_path)
    api_config.settings.evidence_dir = str(evidence_dir)
    api_config.settings.config_dir = str(config_dir)
    api_auth.API_KEY = "test-key"

    import api.app as api_app
    importlib.reload(api_app)
    return api_app.app


def _insert_contract(repo, tmp_dir: Path) -> str:
    """Insert a test contract and return its contract_id."""
    from core.ids import new_ulid
    from core.time import utc_now_iso_z
    contract_id = new_ulid()
    now = utc_now_iso_z()
    with repo.transaction() as conn:
        conn.execute(
            """
            INSERT INTO contracts(
                contract_id, contract_ref, lpo_no, lpo_date,
                buyer_id, vendor_of_record_id, operator_id,
                source_id, processor_id, lane, currency,
                issue_date, due_date, due_terms,
                expected_total_qty, expected_total_qty_kg, expected_total_value,
                lpo_state, created_at, updated_at
            ) VALUES (
                ?, 'LPO-API-TEST', 'LPO-API-001', '2026-01-01',
                'buyer_nycil', 'ananta_flows', 'guildgate',
                'ananta_flows', 'processor_partner_refinery', 'B', 'NGN',
                '2026-01-01', '2026-03-01', '60 days',
                30000.0, 30000000, 68100000.0,
                'ACTIVE', ?, ?
            )
            """,
            (contract_id, now, now),
        )
    return contract_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMerchantPilotAPI(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="api-test-"))
        config_dir = Path(__file__).resolve().parents[1] / "config"
        db_path = self.tmp_dir / ".state" / "drep.sqlite"
        (self.tmp_dir / ".state").mkdir(parents=True, exist_ok=True)
        shutil.copytree(config_dir, self.tmp_dir / "config", dirs_exist_ok=True)

        import os
        os.environ["ANANTA_DB_PATH"] = str(db_path)
        os.environ["ANANTA_EVIDENCE_DIR"] = str(self.tmp_dir / ".state" / "evidence")
        os.environ["ANANTA_CONFIG_DIR"] = str(config_dir)
        os.environ["ANANTA_API_KEY"] = "test-key"

        import importlib
        import api.config as api_config
        import api.auth as api_auth
        api_config.settings.db_path = str(db_path)
        api_config.settings.evidence_dir = str(self.tmp_dir / ".state" / "evidence")
        api_config.settings.config_dir = str(config_dir)
        api_auth.API_KEY = "test-key"

        from adapters.sqlite_repo import SQLiteRepo
        from core.config import RuntimeConfig
        cfg = RuntimeConfig.load(self.tmp_dir)
        self.repo = SQLiteRepo(db_path)
        self.repo.init_db(cfg)

        # Manually set up app.state (bypass lifespan for test)
        import api.app as api_app
        importlib.reload(api_app)
        from domain.event_ledger import EnvelopeValidator
        self.repo._validator = EnvelopeValidator(config_dir / "core_event_requirements.json")
        from api.config import load_catalog_meta
        meta = load_catalog_meta(str(config_dir))

        api_app.app.state.repo = self.repo
        api_app.app.state.meta = meta
        api_app.app.state.evidence_dir = str(self.tmp_dir / ".state" / "evidence")

        self.client = TestClient(api_app.app, raise_server_exceptions=False)
        self.headers = {"X-API-Key": "test-key"}

        self.contract_id = _insert_contract(self.repo, self.tmp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -----------------------------------------------------------------------
    # Test 1: Health returns correct fields
    # -----------------------------------------------------------------------
    def test_1_health_returns_correct_fields(self) -> None:
        """GET /health returns status, version, schema_version, core_requirements_ref,
        core_event_requirements_hash, pilot_contract_ref."""
        resp = self.client.get("/api/v1/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("schema_version", data)
        self.assertIn("core_requirements_ref", data)
        self.assertIn("core_event_requirements_hash", data)
        self.assertIn("pilot_contract_ref", data)
        self.assertIn("v0.1", data["pilot_contract_ref"])

    # -----------------------------------------------------------------------
    # Test 2: Work items list returns data
    # -----------------------------------------------------------------------
    def test_2_work_items_list_returns_data(self) -> None:
        """GET /work-items returns at least one item with data_source=PILOT and canonical trio."""
        resp = self.client.get("/api/v1/work-items", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("items", data)
        self.assertIn("schema_version", data)
        self.assertIn("core_requirements_ref", data)
        self.assertIn("core_event_requirements_hash", data)
        self.assertGreater(len(data["items"]), 0)
        item = data["items"][0]
        self.assertEqual(item["data_source"], "PILOT")
        self.assertIn("schema_version", item)
        self.assertIn("core_requirements_ref", item)
        self.assertIn("core_event_requirements_hash", item)

    # -----------------------------------------------------------------------
    # Test 3: Single work item detail
    # -----------------------------------------------------------------------
    def test_3_single_work_item_detail(self) -> None:
        """GET /work-items/{id} returns entity with available_actions."""
        resp = self.client.get(
            f"/api/v1/work-items/{self.contract_id}", headers=self.headers
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], self.contract_id)
        self.assertIn("available_actions", data)
        self.assertIsInstance(data["available_actions"], list)
        self.assertGreater(len(data["available_actions"]), 0)
        self.assertIn("schema_version", data)
        self.assertIn("evidence_summary", data)

    # -----------------------------------------------------------------------
    # Test 4: Apply transition success
    # -----------------------------------------------------------------------
    def test_4_apply_transition_success(self) -> None:
        """POST /actions/apply → receipt with §2.2 fields + applied=true + deduped=false."""
        idem_key = f"ui:{self.contract_id}:terms:test-{uuid.uuid4()}"
        payload = {
            "trade_id": str(uuid.uuid4()),
            "payload": {
                "actor_org_id": str(uuid.uuid4()),
                "payment_terms": "NET30",
                "delivery_term": "DAP",
                "delivery_location": "Lagos, Nigeria",
            },
        }
        body = {
            "work_item_id": self.contract_id,
            "action_type": "TRANSITION",
            "event_type": "TERMS_SUBMITTED",
            "new_state": "ACTIVE",
            "payload": payload,
            "idempotency_key": idem_key,
        }
        resp = self.client.post("/api/v1/actions/apply", json=body, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        # §2.2 required fields
        for field in ["event_id", "event_type", "entity_type", "entity_id",
                      "content_hash", "idempotency_key", "schema_ok", "created_at"]:
            self.assertIn(field, data, f"Missing §2.2 field: {field}")
        # API enrichments
        self.assertTrue(data["applied"])
        self.assertFalse(data["deduped"])
        self.assertEqual(data["data_source"], "PILOT")
        # new_state present and matches what was requested
        self.assertIn("new_state", data)
        self.assertEqual(data["new_state"], "ACTIVE")
        # canonical trio
        self.assertIn("schema_version", data)
        self.assertIn("core_requirements_ref", data)
        self.assertIn("core_event_requirements_hash", data)

    # -----------------------------------------------------------------------
    # Test 5: Apply transition dedup
    # -----------------------------------------------------------------------
    def test_5_apply_transition_dedup(self) -> None:
        """Same idempotency_key + same payload → deduped=true, no new event row."""
        idem_key = f"ui:{self.contract_id}:terms:dedup-{uuid.uuid4()}"
        payload = {
            "trade_id": str(uuid.uuid4()),
            "payload": {
                "actor_org_id": str(uuid.uuid4()),
                "payment_terms": "NET60",
                "delivery_term": "CIF",
                "delivery_location": "Abuja, Nigeria",
            },
        }
        body = {
            "work_item_id": self.contract_id,
            "action_type": "TRANSITION",
            "event_type": "TERMS_SUBMITTED",
            "new_state": "ACTIVE",
            "payload": payload,
            "idempotency_key": idem_key,
        }
        # First call
        resp1 = self.client.post("/api/v1/actions/apply", json=body, headers=self.headers)
        self.assertEqual(resp1.status_code, 200)
        event_id_1 = resp1.json()["event_id"]

        # Count events before dedup
        conn = self.repo._connect()
        count_before = conn.execute(
            "SELECT COUNT(*) as c FROM event_log WHERE idempotency_key = ?", (idem_key,)
        ).fetchone()["c"]
        conn.close()

        # Second call (dedup)
        resp2 = self.client.post("/api/v1/actions/apply", json=body, headers=self.headers)
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertTrue(data2["deduped"])
        self.assertEqual(data2["event_id"], event_id_1)

        # No new event row
        conn = self.repo._connect()
        count_after = conn.execute(
            "SELECT COUNT(*) as c FROM event_log WHERE idempotency_key = ?", (idem_key,)
        ).fetchone()["c"]
        conn.close()
        self.assertEqual(count_before, count_after)

    # -----------------------------------------------------------------------
    # Test 6: Apply transition conflict
    # -----------------------------------------------------------------------
    def test_6_apply_transition_conflict(self) -> None:
        """Same idempotency_key + different payload → 409."""
        idem_key = f"ui:{self.contract_id}:terms:conflict-{uuid.uuid4()}"
        payload_a = {
            "trade_id": str(uuid.uuid4()),
            "payload": {
                "actor_org_id": str(uuid.uuid4()),
                "payment_terms": "NET30",
                "delivery_term": "DAP",
                "delivery_location": "Lagos",
            },
        }
        payload_b = {
            "trade_id": str(uuid.uuid4()),
            "payload": {
                "actor_org_id": str(uuid.uuid4()),
                "payment_terms": "NET90",  # different
                "delivery_term": "FOB",
                "delivery_location": "Port Harcourt",
            },
        }
        body_a = {
            "work_item_id": self.contract_id,
            "action_type": "TRANSITION",
            "event_type": "TERMS_SUBMITTED",
            "new_state": "ACTIVE",
            "payload": payload_a,
            "idempotency_key": idem_key,
        }
        body_b = {**body_a, "payload": payload_b}

        resp1 = self.client.post("/api/v1/actions/apply", json=body_a, headers=self.headers)
        self.assertEqual(resp1.status_code, 200)

        resp2 = self.client.post("/api/v1/actions/apply", json=body_b, headers=self.headers)
        self.assertEqual(resp2.status_code, 409)
        detail = resp2.json()["detail"]
        self.assertIn("idempotency_key", detail)

    # -----------------------------------------------------------------------
    # Test 7: Apply transition validation error
    # -----------------------------------------------------------------------
    def test_7_apply_transition_validation_error(self) -> None:
        """Bad payload (missing required fields) → 422 with error details."""
        body = {
            "work_item_id": self.contract_id,
            "action_type": "TRANSITION",
            "event_type": "TERMS_SUBMITTED",
            "new_state": "ACTIVE",
            "payload": {"incomplete": "data"},  # missing required fields
            "idempotency_key": f"ui:{self.contract_id}:terms:bad-{uuid.uuid4()}",
        }
        resp = self.client.post("/api/v1/actions/apply", json=body, headers=self.headers)
        self.assertEqual(resp.status_code, 422)
        detail = resp.json()["detail"]
        self.assertIn("errors", detail)
        self.assertIsInstance(detail["errors"], list)
        self.assertGreater(len(detail["errors"]), 0)

    # -----------------------------------------------------------------------
    # Test 8: Evidence upload (+ dedup + entity 404 + link canonical trio)
    # -----------------------------------------------------------------------
    def test_8_evidence_upload(self) -> None:
        """POST /evidence with file → receipt + evidence_ref block.

        Also covers:
        - Same idempotency_key twice → deduped=true on second call (FIX 2)
        - Nonexistent entity → 404 (FIX 4)
        - GET /evidence/{id}/link → includes canonical trio (FIX 3)
        """
        idem_key = f"ui:{self.contract_id}:evidence:{uuid.uuid4()}"
        file_content = b"Test evidence document content"
        common_data = {
            "entity_type": "trade",
            "entity_id": self.contract_id,
            "evidence_kind": "invoice",
            "idempotency_key": idem_key,
            "note": "Test evidence upload",
        }

        # First upload
        resp = self.client.post(
            "/api/v1/evidence",
            headers=self.headers,
            data=common_data,
            files={"file": ("test_invoice.pdf", file_content, "application/pdf")},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("event_id", data)
        self.assertTrue(data["applied"])
        self.assertFalse(data["deduped"])
        self.assertEqual(data["data_source"], "PILOT")
        self.assertIn("evidence_ref", data)
        ev_ref = data["evidence_ref"]
        for field in ["evidence_id", "storage_ref", "filename", "content_hash", "size_bytes"]:
            self.assertIn(field, ev_ref, f"Missing evidence_ref field: {field}")
        self.assertEqual(ev_ref["size_bytes"], len(file_content))
        evidence_id = ev_ref["evidence_id"]

        # Second upload — same key → deduped (FIX 2: deterministic id, pre-flight check)
        resp2 = self.client.post(
            "/api/v1/evidence",
            headers=self.headers,
            data=common_data,
            files={"file": ("test_invoice.pdf", file_content, "application/pdf")},
        )
        self.assertEqual(resp2.status_code, 200, resp2.text)
        data2 = resp2.json()
        self.assertTrue(data2["deduped"])
        self.assertEqual(data2["event_id"], data["event_id"])

        # Nonexistent entity → 404 (FIX 4)
        resp3 = self.client.post(
            "/api/v1/evidence",
            headers=self.headers,
            data={**common_data, "entity_id": "no-such-entity"},
            files={"file": ("x.pdf", b"x", "application/pdf")},
        )
        self.assertEqual(resp3.status_code, 404)

        # GET /evidence/{id}/link includes canonical trio (FIX 3)
        resp4 = self.client.get(
            f"/api/v1/evidence/{evidence_id}/link", headers=self.headers
        )
        self.assertEqual(resp4.status_code, 200, resp4.text)
        link_data = resp4.json()
        for trio_field in ["schema_version", "core_requirements_ref", "core_event_requirements_hash"]:
            self.assertIn(trio_field, link_data, f"Missing canonical trio field: {trio_field}")
        self.assertEqual(link_data["evidence_id"], evidence_id)
        self.assertIn("url", link_data)

    # -----------------------------------------------------------------------
    # Test 9: Event trace
    # -----------------------------------------------------------------------
    def test_9_event_trace(self) -> None:
        """GET /events/{entity_id} → events ordered by created_at ascending."""
        # Apply two events to have multiple
        for i in range(2):
            payload = {
                "trade_id": str(uuid.uuid4()),
                "payload": {
                    "actor_org_id": str(uuid.uuid4()),
                    "payment_terms": "NET30",
                    "delivery_term": "DAP",
                    "delivery_location": "Lagos",
                },
            }
            self.repo.apply_transition(
                event_type="TERMS_SUBMITTED",
                entity_type="trade",
                entity_id=self.contract_id,
                payload=payload,
                table="contracts",
                pk_column="contract_id",
                state_column="lpo_state",
                new_state="ACTIVE",
                idempotency_key=f"test:trace:{self.contract_id}:{i}:{uuid.uuid4()}",
            )

        resp = self.client.get(
            f"/api/v1/events/{self.contract_id}", headers=self.headers
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("events", data)
        self.assertIn("schema_version", data)
        events = data["events"]
        self.assertGreaterEqual(len(events), 2)

        # Verify ascending order
        created_ats = [e["created_at"] for e in events]
        self.assertEqual(created_ats, sorted(created_ats))

        # Each event has required fields
        for ev in events:
            self.assertIn("event_id", ev)
            self.assertIn("event_type", ev)
            self.assertIn("entity_id", ev)
            self.assertIn("created_at", ev)

    # -----------------------------------------------------------------------
    # Test 10: Auth required
    # -----------------------------------------------------------------------
    def test_10_auth_required(self) -> None:
        """Requests without X-API-Key → 401. /health is exempt."""
        # Work items requires auth
        resp = self.client.get("/api/v1/work-items")
        self.assertEqual(resp.status_code, 401)

        # Actions requires auth
        resp = self.client.post("/api/v1/actions/apply", json={})
        self.assertEqual(resp.status_code, 401)

        # Health does NOT require auth
        resp = self.client.get("/api/v1/health")
        self.assertEqual(resp.status_code, 200)

    # -----------------------------------------------------------------------
    # Test 11: No settlement fields
    # -----------------------------------------------------------------------
    def test_11_no_settlement_fields(self) -> None:
        """None of §1.2 forbidden fields appear in any response."""
        # Health
        resp = self.client.get("/api/v1/health")
        violations = _no_forbidden(resp.json())
        self.assertEqual(violations, [], f"Settlement fields in /health: {violations}")

        # Work items
        resp = self.client.get("/api/v1/work-items", headers=self.headers)
        violations = _no_forbidden(resp.json())
        self.assertEqual(violations, [], f"Settlement fields in /work-items: {violations}")

        # Single work item
        resp = self.client.get(
            f"/api/v1/work-items/{self.contract_id}", headers=self.headers
        )
        violations = _no_forbidden(resp.json())
        self.assertEqual(violations, [], f"Settlement fields in work-item detail: {violations}")

        # Action apply receipt
        payload = {
            "trade_id": str(uuid.uuid4()),
            "payload": {
                "actor_org_id": str(uuid.uuid4()),
                "payment_terms": "NET30",
                "delivery_term": "DAP",
                "delivery_location": "Lagos",
            },
        }
        body = {
            "work_item_id": self.contract_id,
            "action_type": "TRANSITION",
            "event_type": "TERMS_SUBMITTED",
            "new_state": "ACTIVE",
            "payload": payload,
            "idempotency_key": f"ui:{self.contract_id}:fence:{uuid.uuid4()}",
        }
        resp = self.client.post("/api/v1/actions/apply", json=body, headers=self.headers)
        violations = _no_forbidden(resp.json())
        self.assertEqual(violations, [], f"Settlement fields in action receipt: {violations}")

    # -----------------------------------------------------------------------
    # Test 12: Entity not found — apply, notes, evidence all return 404
    # -----------------------------------------------------------------------
    def test_12_entity_not_found(self) -> None:
        """Apply / notes / evidence upload on nonexistent entity → 404 (FIX 4)."""
        # actions/apply → 404
        body = {
            "work_item_id": "nonexistent-entity-id-xyz",
            "action_type": "TRANSITION",
            "event_type": "TERMS_SUBMITTED",
            "new_state": "ACTIVE",
            "payload": {},
            "idempotency_key": f"ui:nonexistent:test:{uuid.uuid4()}",
        }
        resp = self.client.post("/api/v1/actions/apply", json=body, headers=self.headers)
        self.assertEqual(resp.status_code, 404)

        # notes → 404
        note_body = {
            "entity_type": "trade",
            "entity_id": "no-such-contract",
            "note": "This should 404",
            "idempotency_key": f"cli:nonexistent:note:{uuid.uuid4()}",
        }
        resp2 = self.client.post("/api/v1/notes", json=note_body, headers=self.headers)
        self.assertEqual(resp2.status_code, 404)

    # -----------------------------------------------------------------------
    # Test 13: Evidence list returns items for a known entity
    # -----------------------------------------------------------------------
    def test_13_evidence_list_returns_items(self) -> None:
        """GET /evidence?entity_id=X returns EvidenceItem[] with required fields."""
        # Upload evidence first so there's at least one item
        idem_key = f"ui:{self.contract_id}:ev-list:{uuid.uuid4()}"
        file_content = b"Evidence list test document"
        upload_resp = self.client.post(
            "/api/v1/evidence",
            headers=self.headers,
            data={
                "entity_type": "trade",
                "entity_id": self.contract_id,
                "evidence_kind": "waybill",
                "idempotency_key": idem_key,
                "note": "list test note",
            },
            files={"file": ("waybill.pdf", file_content, "application/pdf")},
        )
        self.assertEqual(upload_resp.status_code, 200, upload_resp.text)

        # Now list evidence for this entity
        resp = self.client.get(
            f"/api/v1/evidence?entity_id={self.contract_id}", headers=self.headers
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()

        # canonical trio present
        self.assertIn("schema_version", data)
        self.assertIn("core_requirements_ref", data)
        self.assertIn("core_event_requirements_hash", data)
        self.assertEqual(data["entity_id"], self.contract_id)
        self.assertGreater(data["total"], 0)

        # Each item has required EvidenceItem fields
        item = data["items"][0]
        for field in ["evidence_id", "evidence_kind", "status", "filename", "submitted_at"]:
            self.assertIn(field, item, f"Missing EvidenceItem field: {field}")
        self.assertEqual(item["evidence_kind"], "waybill")
        self.assertEqual(item["status"], "UNLINKED")


if __name__ == "__main__":
    unittest.main()
