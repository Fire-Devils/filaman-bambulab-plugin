import importlib
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Color, Filament, FilamentColor, Manufacturer, Printer
from app.models.base import Base
from app.models.location import Location
from app.models.printer import PrinterSlot
from app.models.printer_params import FilamentPrinterParam
from app.models.spool import Spool, SpoolStatus
from app.models.system_extra_field import SystemExtraField


DRIVER_MODULE = importlib.import_module("bambulab.driver")
STATE_MODULE = importlib.import_module("bambulab.state")
PendingSpool = STATE_MODULE.PendingSpool
CATALOG_MODULE = importlib.import_module("bambulab.catalog")
ENRICHMENT_MODULE = importlib.import_module("bambulab.catalog_enrichment")
SPOOL_SYNC_MODULE = importlib.import_module("bambulab.spool_sync")
Driver = DRIVER_MODULE.Driver


def make_driver(printer_id=1, **config):
    events = []
    driver = Driver(
        printer_id=printer_id,
        config={"printer_model": "P1S", **config},
        emitter=events.append,
    )
    driver._running = True
    return driver, events


class IdentifierTests(unittest.TestCase):
    def test_normalizes_valid_tag_uid(self):
        self.assertEqual(
            Driver._normalize_hex_identifier("a1:b2:c3:d4:e5:f6:01:02", 16),
            "A1B2C3D4E5F60102",
        )

    def test_rejects_zero_placeholder(self):
        self.assertIsNone(
            Driver._normalize_hex_identifier("0000000000000000", 16)
        )
        self.assertIsNone(
            Driver._normalize_hex_identifier("0" * 32, 32)
        )

    def test_rejects_wrong_length(self):
        self.assertIsNone(Driver._normalize_hex_identifier("A1B2C3D4", 16))

    def test_zero_is_valid_remaining_percentage(self):
        self.assertEqual(Driver._parse_percentage(0), 0)

    def test_calculates_bambu_estimated_remaining_weight(self):
        self.assertEqual(
            Driver._estimated_remaining_weight(
                {"tray_weight": "1000", "remain": 49}
            ),
            490,
        )
        self.assertEqual(
            Driver._estimated_remaining_weight(
                {"tray_weight": "1000", "remain": 0}
            ),
            0,
        )

    def test_rejects_unknown_bambu_weight_estimate(self):
        self.assertIsNone(
            Driver._estimated_remaining_weight(
                {"tray_weight": "0", "remain": -1}
            )
        )

    def test_spool_weight_sync_defaults_to_disabled(self):
        driver, _ = make_driver(auto_import_spools=True)

        self.assertFalse(driver._sync_spool_weight)
        self.assertFalse(driver.health()["sync_spool_weight"])


class ShopImageParsingTests(unittest.TestCase):
    def test_store_search_uses_highlighted_product_media_not_color_palette(self):
        payload = {
            "code": 1,
            "data": {
                "page": {
                    "total": "1",
                    "records": [
                        {
                            "seoCode": "pla-pure",
                            "highlightProductSkuId": "738891519803023418",
                            "mediaFiles": [
                                "https://store.bblcdn.eu/product/pure-white-spool.jpg"
                            ],
                            "colorList": [
                                {
                                    "colorPalette": (
                                        "https://store.bblcdn.eu/palette/"
                                        "Pure_White.jpg"
                                    )
                                }
                            ],
                        }
                    ],
                }
            },
        }

        result = Driver._parse_store_search_product(
            payload,
            "https://eu.store.bambulab.com/products/pla-pure",
        )

        self.assertIn("pure-white-spool.jpg", result["shop_image_url"])
        self.assertNotIn("Pure_White.jpg", result["shop_image_url"])
        self.assertEqual(
            result["shop_source_url"],
            "https://eu.store.bambulab.com/de/products/pla-pure"
            "?id=738891519803023418",
        )

    def test_store_search_requires_one_result_for_the_expected_product(self):
        payload = {
            "code": 1,
            "data": {
                "page": {
                    "total": "2",
                    "records": [
                        {
                            "seoCode": "pla-pure",
                            "highlightProductSkuId": "123",
                            "mediaFiles": ["https://store.bblcdn.eu/image.jpg"],
                        }
                    ],
                }
            },
        }

        self.assertIsNone(Driver._parse_store_search_product(payload))

    def test_extracts_and_optimizes_product_image_from_json_ld(self):
        html = """
        <html><head><script type="application/ld+json">
        {
          "@type": "ProductGroup",
          "hasVariant": [
            {
              "@type": "Product",
              "name": "PLA Matte - Charcoal (11101) / Refill / 1kg",
              "image": "https://store.bblcdn.eu/s8/default/hash/charcoal.png"
            }
          ]
        }
        </script></head></html>
        """

        images = Driver._extract_shop_product_images(html)

        self.assertIn("11101", images)
        self.assertEqual(
            images["11101"],
            "https://store.bblcdn.eu/s8/default/hash/charcoal.png"
            "__op__resize,m_lfit,w_640__op__format,f_auto__op__quality,q_80",
        )

    def test_prefers_current_storefront_color_swatch(self):
        html = """
        <li value="Pure White (17100)" class="color-option">
          <img src="https://store.bblcdn.eu/s8/default/hash/Pure_White.jpg">
        </li>
        <script type="application/ld+json">
        {
          "@type": "Product",
          "name": "PLA Pure - Pure White (17100) / Refill / 1kg",
          "image": "https://store.bblcdn.eu/s8/default/hash/generic.jpg"
        }
        </script>
        """

        images = Driver._extract_shop_product_images(html)

        self.assertEqual(
            images["17100"],
            "https://store.bblcdn.eu/s8/default/hash/Pure_White.jpg"
            "__op__resize,m_lfit,w_640__op__format,f_auto__op__quality,q_80",
        )

    def test_rejects_non_bambu_product_and_image_hosts(self):
        self.assertIsNone(Driver._allowed_shop_url("https://example.com/pla"))
        self.assertIsNone(
            Driver._allowed_shop_image_url("https://example.com/charcoal.png")
        )

    def test_health_only_exposes_metadata_for_current_slot_identity(self):
        driver, _ = make_driver(resolve_shop_images=True)
        slot = {
            "slot_index": "0-0",
            "tray_type": "PLA",
            "tray_color": "000000FF",
            "tray_uuid": "AABBCCDDEEFF0011AABBCCDDEEFF0011",
        }
        driver._current_slots = [slot]
        driver._slot_display_metadata["0-0"] = {
            "_slot_identity": driver._slot_identity(slot),
            "shop_image_url": "https://store.bblcdn.eu/image.png",
        }

        self.assertEqual(
            driver.health()["slots"][0]["shop_image_url"],
            "https://store.bblcdn.eu/image.png",
        )

        driver._current_slots[0]["tray_uuid"] = "11223344556677881122334455667788"
        self.assertNotIn("shop_image_url", driver.health()["slots"][0])

    def test_health_counts_every_external_tray_like_the_ams_info_it_reuses(self):
        driver, _ = make_driver()
        driver._current_ams_units = [{"tray_count": 4}]
        driver._current_slots = [
            {"slot_index": "0-0", "tray_type": "PLA"},
            {"slot_index": "255-254", "tray_type": "PLA"},
            {"slot_index": "255-255", "tray_type": "PLA"},
        ]

        health = driver.health()

        self.assertEqual(health["slot_count"], 6)
        self.assertTrue(health["external_spool"])
        self.assertEqual(health["ams_count"], 1)
        self.assertEqual(
            health["slot_count"], driver._build_ams_info(driver._current_slots)["slot_count"]
        )


class PluginPageTests(unittest.TestCase):
    def test_manifest_registers_custom_navigation_page(self):
        plugin_dir = Path(__file__).resolve().parents[1] / "bambulab"
        manifest = json.loads((plugin_dir / "plugin.json").read_text())

        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+(?:-dev\.[1-9]\d*)?$")
        self.assertEqual(manifest["page_url"], "/plugin-page/bambulab")
        self.assertTrue(manifest["show_in_nav"])
        self.assertFalse(
            manifest["config_schema"]["properties"]["sync_spool_weight"]["default"]
        )
        self.assertTrue((plugin_dir / "page.html").is_file())

    def test_spool_gallery_prefers_article_number_for_product_code(self):
        plugin_dir = Path(__file__).resolve().parents[1] / "bambulab"
        page = (plugin_dir / "page.html").read_text()

        self.assertIn("fields.article_number || slot.bambu_product_code", page)

    def test_spool_gallery_requests_primary_driver_image_refresh(self):
        plugin_dir = Path(__file__).resolve().parents[1] / "bambulab"
        page = (plugin_dir / "page.html").read_text()

        self.assertIn("const loadInventory = async (refreshImages = false)", page)
        self.assertIn("/driver/health?refresh=1", page)
        self.assertIn("const loadDriverHealth = async", page)
        self.assertIn("catch { return requestJson(url); }", page)
        self.assertIn("Promise.allSettled", page)
        self.assertIn("else loadInventory(true);", page)
        self.assertIn("loadInventory(true));", page)

    def test_plugin_page_relies_on_filaman_navigation_and_uses_user_language(self):
        plugin_dir = Path(__file__).resolve().parents[1] / "bambulab"
        page = (plugin_dir / "page.html").read_text()

        self.assertNotIn('class="fm-sidebar"', page)
        self.assertNotIn('class="plugin-main"', page)
        self.assertNotIn('id="back-button"', page)
        self.assertNotIn("window.history.back()", page)
        self.assertNotIn("'page.back':", page)
        self.assertIn("/api/v1/me", page)
        self.assertIn("localStorage.getItem('lang')", page)
        self.assertIn("const translations = {", page)
        self.assertIn("en: {", page)
        self.assertIn("de: {", page)
        self.assertIn('data-i18n="page.title"', page)


class ModuleLayoutTests(unittest.TestCase):
    """Protect the responsibility boundaries of the split driver package."""

    def test_driver_delegates_feature_areas_to_focused_modules(self):
        self.assertEqual(Driver.__module__, "bambulab.driver")
        self.assertEqual(Driver._process_slots.__module__, "bambulab.slots")
        self.assertEqual(
            Driver._auto_import_rfid_spools.__module__, "bambulab.spool_sync"
        )
        self.assertEqual(
            Driver._cache_shop_image_for_filament.__module__, "bambulab.catalog"
        )
        self.assertEqual(
            Driver._register_inventory_enrichment.__module__,
            "bambulab.catalog_enrichment",
        )


class CacheEvictionTests(unittest.TestCase):
    def test_evict_expired_drops_only_stale_entries(self):
        cache = {"fresh": 100.0, "stale": 10.0}
        CATALOG_MODULE._evict_expired(cache, ttl=60, now=100.0)
        self.assertEqual(cache, {"fresh": 100.0})

    def test_evict_expired_timestamped_drops_only_stale_entries(self):
        cache = {
            "fresh": (100.0, {"url": "a"}),
            "stale": (10.0, {"url": "b"}),
        }
        CATALOG_MODULE._evict_expired_timestamped(cache, ttl=60, now=100.0)
        self.assertEqual(cache, {"fresh": (100.0, {"url": "a"})})


class InventoryEnrichmentCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_debounce = (
            ENRICHMENT_MODULE.INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS
        )
        self.original_interval = ENRICHMENT_MODULE.INVENTORY_IMAGE_SCAN_SECONDS
        ENRICHMENT_MODULE.INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS = 0.01
        ENRICHMENT_MODULE.INVENTORY_IMAGE_SCAN_SECONDS = 60
        self.drivers = []

    async def asyncTearDown(self):
        for driver in list(Driver._inventory_enrichment_instances):
            await driver._unregister_inventory_enrichment()
        ENRICHMENT_MODULE.INVENTORY_IMAGE_EVENT_DEBOUNCE_SECONDS = self.original_debounce
        ENRICHMENT_MODULE.INVENTORY_IMAGE_SCAN_SECONDS = self.original_interval

    async def _wait_for(self, predicate, timeout=1.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Timed out waiting for inventory enrichment")
            await asyncio.sleep(0.01)

    async def test_inventory_events_are_debounced_and_shared_by_all_drivers(self):
        first, _ = make_driver(printer_id=1, resolve_shop_images=True)
        second, _ = make_driver(printer_id=2, resolve_shop_images=True)
        self.drivers = [first, second]
        calls = []

        async def refresh_first():
            calls.append(first.printer_id)
            return {"filaments": 1, "images": 1}

        async def refresh_second():
            calls.append(second.printer_id)
            return {"filaments": 1, "images": 1}

        first._refresh_inventory_shop_images = refresh_first
        second._refresh_inventory_shop_images = refresh_second

        await first._register_inventory_enrichment()
        await second._register_inventory_enrichment()
        await self._wait_for(lambda: len(calls) == 1)
        self.assertEqual(calls, [1])
        self.assertEqual(len(Driver._inventory_enrichment_instances), 2)

        calls.clear()
        await ENRICHMENT_MODULE.event_bus.publish({"event": "locations_changed"})
        await asyncio.sleep(0.05)
        self.assertEqual(calls, [])

        await ENRICHMENT_MODULE.event_bus.publish({"event": "spools_changed"})
        await ENRICHMENT_MODULE.event_bus.publish({"event": "filaments_changed"})
        await self._wait_for(lambda: len(calls) == 1)
        await asyncio.sleep(0.05)
        self.assertEqual(calls, [1])

        calls.clear()
        await first._unregister_inventory_enrichment()
        await self._wait_for(lambda: len(calls) == 1)
        self.assertEqual(calls, [2])

        await second._unregister_inventory_enrichment()
        self.assertEqual(Driver._inventory_enrichment_instances, set())
        self.assertIsNone(Driver._inventory_enrichment_listener_task)
        self.assertIsNone(Driver._inventory_enrichment_worker_task)

    async def test_explicit_driver_refresh_reconciles_inventory_images(self):
        driver, _ = make_driver(printer_id=1, resolve_shop_images=True)
        calls = []

        async def refresh_images():
            calls.append(True)
            return {"filaments": 14, "images": 12}

        driver._refresh_inventory_shop_images = refresh_images

        result = await driver.refresh_status()

        self.assertEqual(calls, [True])
        self.assertEqual(
            result,
            {"catalog_images": {"filaments": 14, "images": 12}},
        )

    async def test_explicit_driver_refresh_is_inert_when_images_are_disabled(self):
        driver, _ = make_driver(printer_id=1, resolve_shop_images=False)

        async def unexpected_refresh():
            self.fail("disabled catalog images must not scan inventory")

        driver._refresh_inventory_shop_images = unexpected_refresh

        self.assertEqual(await driver.refresh_status(), {})

    async def test_explicit_driver_refresh_failure_does_not_break_health(self):
        driver, _ = make_driver(printer_id=1, resolve_shop_images=True)

        async def failed_refresh():
            raise RuntimeError("image service offline")

        driver._refresh_inventory_shop_images = failed_refresh

        with self.assertLogs(level="WARNING"):
            self.assertEqual(await driver.refresh_status(), {})


class SlotProcessingTests(unittest.TestCase):
    def test_clearing_material_keeps_the_slot_occupied_and_assignable(self):
        driver, events = make_driver(printer_model="H2C")
        driver._update_spool_location = AsyncMock()
        driver._send_filament_setting = Mock()
        driver._schedule_slot_location_release = Mock()
        tray = {"id": "3", "state": 11, "tray_type": "PLA"}
        ams = {"ams": [{"id": "0", "tray": [tray]}]}
        driver._process_slots({"print": {"ams": {**ams, "tray_exist_bits": "bef"}}})
        filament_data = {"material_type": "PLA"}
        asyncio.run(driver.send_filament_to_tray(0, 3, filament_data, spool_id=17))

        driver._pending = PendingSpool(99, filament_data, None)
        tray.update(tray_type="", tray_info_idx="", tray_color="00000000")
        del tray["state"]
        driver._process_slots({"print": {"ams": ams}})

        slot = driver.health()["slots"][0]
        self.assertTrue(slot["present"])
        self.assertEqual(slot["tray_type"], "")
        self.assertEqual(slot["spool_id"], 17)
        self.assertEqual(driver._pending.spool_id, 99)
        driver._schedule_slot_location_release.assert_not_called()
        driver._send_filament_setting.assert_called_once_with(0, 3, filament_data)

        asyncio.run(driver.send_filament_to_tray(0, 3, filament_data, spool_id=42))
        self.assertEqual(events[-1]["slots"][0]["spool_id"], 42)

    def test_pending_spool_is_assigned_on_insertion_without_material(self):
        for include_trays in (False, True):
            with self.subTest(include_trays=include_trays):
                driver, _ = make_driver(printer_model="H2C")
                driver._send_filament_setting = Mock()
                driver._process_slots({"print": {"ams": {
                    "tray_exist_bits": "0",
                    "ams": [{"id": "0", "tray": [{"id": "3", "state": 0}]}],
                }}})
                filament_data = {"material_type": "PETG"}
                driver._pending = PendingSpool(17, filament_data, "0-3")
                ams = {"tray_exist_bits": "8"}
                if include_trays:
                    ams["ams"] = [{"id": "0", "tray": [
                        {"id": "3", "state": 11, "tray_type": ""},
                    ]}]
                driver._process_slots({"print": {"ams": ams}})
                driver._process_slots({"print": {"ams": ams}})

                slot = driver.health()["slots"][0]
                self.assertTrue(slot["present"])
                self.assertEqual(slot["spool_id"], 17)
                self.assertIsNone(driver._pending)
                driver._send_filament_setting.assert_called_once_with(0, 3, filament_data)

    def test_ams_ht_presence_delta_releases_the_removed_spool(self):
        driver, _ = make_driver(printer_model="H2D")
        driver._schedule_slot_location_release = Mock()
        uuid = "A" * 32
        driver._spool_ids_by_tray_uuid[uuid] = 17
        driver._process_slots({"print": {
            "ams": {"tray_exist_bits": "5000b", "ams": [
                {"id": "128", "tray": [{"id": "0", "tray_type": "PLA", "tray_uuid": uuid}]},
                {"id": "129", "tray": [{"id": "0", "state": 10, "tray_type": "PLA"}]},
                {"id": "130", "tray": [{"id": "0", "state": 11, "tray_type": ""}]},
            ]},
            "vt_tray": {"id": "254", "tray_type": "PETG"},
        }})
        first, second, third, external = driver.health()["slots"]
        self.assertEqual(first["spool_id"], 17)
        self.assertFalse(second["present"])
        self.assertTrue(third["present"])
        driver._schedule_slot_location_release.reset_mock()

        driver._process_slots({"print": {"ams": {"tray_exist_bits": "4000b"}}})
        removed, _, _, after = driver.health()["slots"]
        self.assertFalse(removed["present"])
        self.assertIsNone(removed["spool_id"])
        self.assertFalse(removed.get("tray_type"))
        self.assertEqual(after, external)
        driver._schedule_slot_location_release.assert_called_once_with("128-0")

        driver._process_slots({"print": {"ams": {"tray_exist_bits": "5000b"}}})
        reinserted = driver.health()["slots"][0]
        self.assertTrue(reinserted["present"])
        self.assertIsNone(reinserted.get("spool_id"))

    def test_ams_presence_falls_back_to_state_then_material(self):
        driver, _ = make_driver()
        driver._process_slots({"print": {"ams": {
            "tray_exist_bits": "invalid",
            "ams": [{"id": "0", "tray": [
                {"id": "0", "state": 11, "tray_type": ""},
                {"id": "1", "state": "10", "tray_type": "PLA"},
                {"id": "2", "tray_type": "PLA"},
                {"id": "3"},
            ]}],
        }}})
        self.assertEqual(
            [slot["present"] for slot in driver.health()["slots"]],
            [True, False, True, False],
        )

    def test_p2s_shutdown_preserves_loaded_slots_but_real_zero_mask_releases_them(self):
        for model, powered, releases in (
            ("P2S", False, False), ("P2S", True, True), ("X1C", False, True),
        ):
            with self.subTest(model=model, powered=powered):
                driver, _ = make_driver(printer_model=model)
                driver._schedule_slot_location_release = Mock()
                driver._process_slots({"print": {"ams": {
                    "tray_exist_bits": "1",
                    "ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]}],
                }}})
                driver._slot_spool_ids["0-0"] = 17
                driver._process_slots({"print": {"ams": {
                    "tray_exist_bits": "0", "power_on_flag": powered,
                }}})

                slot = driver.health()["slots"][0]
                self.assertEqual(slot["present"], not releases)
                self.assertEqual(driver._schedule_slot_location_release.called, releases)

    def test_p2s_shutdown_mask_only_reconciles_individually_confirmed_empty_trays(self):
        driver, events = make_driver(printer_model="P2S")
        released = []
        driver._schedule_slot_location_release = released.append
        driver._process_slots({"print": {"ams": {
            "ams_exist_bits": "1", "tray_exist_bits": "3",
            "ams": [{"id": "0", "tray": [
                {"id": "0", "state": 11, "tray_type": "PLA"},
                {"id": "1", "state": 11, "tray_type": "PETG"},
            ]}],
        }}})
        driver._process_slots({"print": {"ams": {
            "ams_exist_bits": "0", "tray_exist_bits": "0", "power_on_flag": False,
            "ams": [{"id": "0", "tray": [
                {"id": "0", "tray_type": ""}, {"id": "1", "state": 8},
            ]}],
        }}})
        self.assertEqual([slot["present"] for slot in events[-1]["slots"]], [True, False])
        self.assertEqual(driver.health()["ams_count"], 1)
        self.assertEqual(released, ["0-1"])

        driver._process_slots({"print": {"ams": {
            "tray_exist_bits": "0", "power_on_flag": False,
            "ams": [{"id": "0", "tray": [{"id": "0", "state": 8}]}],
        }}})
        self.assertEqual([slot["present"] for slot in events[-1]["slots"]], [False, False])
        self.assertEqual(released, ["0-1", "0-0"])

    def test_valid_topology_mask_retires_four_bay_and_ht_units_once(self):
        for update in ([], [{"id": "0", "tray": [{"id": "0", "remain": 55}]}]):
            with self.subTest(update=update):
                driver, events = make_driver()
                released = []
                driver._schedule_slot_location_release = released.append
                driver._process_slots({"print": {"ams": {
                    "ams_exist_bits": "13", "tray_exist_bits": "10011",
                    "ams": [
                        {"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]},
                        {"id": "1", "tray": [{"id": "0", "tray_type": "PETG"}]},
                        {"id": "128", "tray": [{"id": "0", "tray_type": "ASA"}]},
                    ],
                }}})
                driver._process_slots({"print": {"ams": {
                    "ams_exist_bits": "invalid", "ams": update,
                }}})
                self.assertEqual(driver.health()["ams_count"], 3)

                topology = {"ams_exist_bits": "1", "ams": update}
                driver._process_slots({"print": {"ams": topology}})
                health = driver.health()
                self.assertEqual(health["ams_count"], 1)
                self.assertEqual([slot["slot_index"] for slot in health["slots"]], ["0-0"])
                retired = [slot for slot in events[-1]["slots"] if slot.get("is_active") is False]
                self.assertEqual([slot["slot_index"] for slot in retired], ["1-0", "128-0"])
                self.assertTrue(all(not slot["present"] and slot["spool_id"] is None for slot in retired))
                self.assertEqual(released, ["1-0", "128-0"])
                event_count = len(events)
                driver._process_slots({"print": {"ams": topology}})
                self.assertEqual(len(events), event_count)
                self.assertEqual(released, ["1-0", "128-0"])

                driver._process_slots({"print": {"ams": {
                    "ams_exist_bits": "11", "tray_exist_bits": "10001",
                    "ams": [{"id": "128", "tray": [{"id": "0", "tray_type": "ASA"}]}],
                }}})
                self.assertEqual([slot["slot_index"] for slot in driver.health()["slots"]], ["0-0", "128-0"])
                self.assertTrue(events[-1]["slots"][-1]["present"])

    def test_replugged_unit_does_not_inherit_the_pre_replug_occupancy_mask(self):
        for retirement in ({}, {"tray_exist_bits": "1"}):
            with self.subTest(retirement=retirement):
                driver, _ = make_driver()
                driver._process_slots({"print": {"ams": {
                    "ams_exist_bits": "3", "tray_exist_bits": "11",
                    "ams": [
                        {"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]},
                        {"id": "1", "tray": [{"id": "0", "tray_type": "PETG"}]},
                    ],
                }}})
                driver._process_slots({"print": {"ams": {"ams_exist_bits": "1", **retirement}}})
                driver._process_slots({"print": {"ams": {"ams": [
                    {"id": "1", "tray": [{"id": "0", "state": 8}]},
                ]}}})
                self.assertEqual([slot["present"] for slot in driver.health()["slots"]], [True, False])

                driver._process_slots({"print": {"ams": {"ams_exist_bits": "1", "tray_exist_bits": "1"}}})
                driver._process_slots({"print": {"ams": {"tray_exist_bits": "1"}}})
                driver._process_slots({"print": {"ams": {"ams": [
                    {"id": "1", "tray": [{"id": "0", "state": 9}]},
                ]}}})
                self.assertEqual([slot["present"] for slot in driver.health()["slots"]], [True, True])

                driver._process_slots({"print": {"ams": {"ams_exist_bits": "1"}}})
                driver._process_slots({"print": {"ams": {"ams_exist_bits": "3", "tray_exist_bits": "11"}}})
                driver._process_slots({"print": {"ams": {"ams": [
                    {"id": "1", "tray": [{"id": "0", "state": 8}]},
                ]}}})
                self.assertEqual([slot["present"] for slot in driver.health()["slots"]], [True, True])

    def test_partial_ams_update_merges_units_and_trays_and_applies_global_mask(self):
        driver, _ = make_driver()
        released = []
        driver._schedule_slot_location_release = released.append
        uuid = "A" * 32
        driver._spool_ids_by_tray_uuid[uuid] = 17
        driver._process_slots({"print": {"ams": {
            "tray_exist_bits": "13",
            "ams": [
                {"id": "0", "humidity": "3", "tray": [
                    {"id": "0", "tray_type": "PLA", "tray_uuid": uuid},
                    {"id": "1", "tray_type": "PETG"},
                ]},
                {"id": "1", "info": "3", "tray": [{"id": "0", "tray_type": "ASA"}]},
            ],
        }}})
        driver._process_slots({"print": {"ams": {
            "tray_exist_bits": "3",
            "ams": [{"id": "0", "tray": [{"id": "0", "remain": 45}]}],
        }}})

        health = driver.health()
        self.assertEqual(len(health["slots"]), 3)
        first, second, third = health["slots"]
        self.assertEqual((first["tray_type"], first["remain"], first["spool_id"]), ("PLA", 45, 17))
        self.assertEqual(second["tray_type"], "PETG")
        self.assertFalse(third["present"])
        self.assertFalse(third.get("tray_uuid"))
        self.assertEqual(released, ["1-0"])
        units = health["ams_units"]
        self.assertEqual([(u["ams_id"], u["tray_count"]) for u in units], [(0, 2), (1, 1)])
        self.assertEqual(units[0]["humidity"], "3")
        self.assertEqual(units[1]["info"], "3")

    def test_p2s_state_fallback_distinguishes_empty_from_unconfigured(self):
        driver, _ = make_driver(printer_model="P2S")
        driver._process_slots({"print": {"ams": {"ams": [{"id": "0", "tray": [
            {"id": "0", "state": 8, "tray_type": "PLA"},
            {"id": "1", "state": "9"},
            {"id": "2", "state": 999, "tray_type": "PETG"},
            {"id": "3", "state": 3},
        ]}]}}})
        self.assertEqual([slot["present"] for slot in driver.health()["slots"]], [False, True, True, True])

    def test_pending_waits_through_material_echo_and_calibration(self):
        driver, _ = make_driver()
        driver._send_filament_setting = Mock()
        driver._process_slots({"print": {"ams": {
            "tray_exist_bits": "1", "ams": [{"id": "0", "tray": [{"id": "0", "state": 9}]}],
        }}})
        driver._pending = PendingSpool(17, {"material_type": "PETG"})
        driver._process_slots({"print": {"ams": {"ams": [{"id": "0", "tray": [
            {"id": "0", "state": 11, "tray_type": "PLA"},
        ]}]}}})
        driver._process_slots({"print": {"ams": {"ams": [{"id": "0", "tray": [
            {"id": "0", "cali_idx": 4},
        ]}]}}})
        self.assertTrue(driver.health()["pending"])
        driver._send_filament_setting.assert_not_called()

    def test_confirmed_uuid_swap_consumes_pending_before_legacy_metadata_change(self):
        driver, _ = make_driver()
        driver._send_filament_setting = Mock()
        trays = [{"id": "0", "tray_type": "PLA", "tray_uuid": "A" * 32}]
        ams = {"tray_exist_bits": "1", "ams": [{"id": "0", "tray": trays}]}
        external = {"id": "254", "tray_type": "PLA"}
        driver._process_slots({"print": {"ams": ams, "vt_tray": external}})
        driver._pending = PendingSpool(17, {"material_type": "PETG"})
        # Clearing the printer profile can temporarily blank its UUID too.
        trays[0].update(tray_type="", tray_uuid="0" * 32)
        driver._process_slots({"print": {"ams": ams}})
        trays[0].update(tray_type="PLA", tray_uuid="B" * 32)
        driver._process_slots({"print": {"ams": ams, "vt_tray": {**external, "tray_type": "ASA"}}})
        self.assertFalse(driver.health()["pending"])
        driver._send_filament_setting.assert_called_once_with(0, 0, {"material_type": "PETG"})
        self.assertEqual(driver.health()["slots"][0]["spool_id"], 17)

    def test_pending_precedes_later_rfid_until_removal(self):
        driver, _ = make_driver(read_only=True)
        uuid = "A" * 32
        driver._spool_ids_by_tray_uuid[uuid] = 99
        ams = {"ams": [{"id": "0", "tray": [{"id": "0", "state": 8}]}]}
        driver._process_slots({"print": {"ams": ams}})
        driver._pending = PendingSpool(17, {"material_type": "PETG"})
        driver._process_slots({"print": {"ams": {"tray_exist_bits": "1"}}})
        ams["ams"][0]["tray"][0].update(state=11, tray_uuid=uuid, tray_type="PLA")
        driver._process_slots({"print": {"ams": ams}})
        driver._emit_slots_with_spool_ids()
        self.assertEqual(driver.health()["slots"][0]["spool_id"], 17)

        driver._process_slots({"print": {"ams": {"tray_exist_bits": "0"}}})
        driver._process_slots({"print": {"ams": {**ams, "tray_exist_bits": "1"}}})
        self.assertEqual(driver.health()["slots"][0]["spool_id"], 99)

    def test_retains_bambu_rfid_identifiers_in_slot_state(self):
        driver, _ = make_driver(auto_import_spools=False)
        driver._process_slots(
            {
                "print": {
                    "command": "push_status",
                    "ams": {
                        "ams": [
                            {
                                "id": "0",
                                "tray": [
                                    {
                                        "id": "0",
                                        "tray_type": "PLA",
                                        "tray_color": "FF0000FF",
                                        "tray_info_idx": "GFA00",
                                        "tag_uid": "A1B2C3D4E5F60102",
                                        "tray_uuid": "AABBCCDDEEFF0011AABBCCDDEEFF0011",
                                        "tray_weight": "1000",
                                        "remain": 75,
                                    }
                                ],
                            }
                        ]
                    },
                }
            }
        )

        slot = driver._current_slots[0]
        self.assertEqual(slot["tag_uid"], "A1B2C3D4E5F60102")
        self.assertEqual(
            slot["tray_uuid"], "AABBCCDDEEFF0011AABBCCDDEEFF0011"
        )
        self.assertEqual(slot["tray_weight"], "1000")
        self.assertEqual(slot["remain"], 75)

    def test_pending_spool_lands_in_the_slot_even_when_read_only(self):
        """A spool weighed on the scale has to reach the slot assignment.

        The filament setting only tells the printer about material and colour;
        the printer knows nothing about FilaMan spools. Without spool_id on the
        slot the plugin manager has nothing to write, and the tray stays empty
        in FilaMan no matter how often it is scanned.
        """
        driver, _ = make_driver(read_only=True)
        driver._current_slots = [
            {
                "slot_index": "0-0",
                "slot_name": "AMS 1 - Slot 1",
                "tray_type": "",
                "tray_color": "",
                "tray_info_idx": "",
                "present": False,
            }
        ]
        driver._pending = PendingSpool(17, {"material_type": "PETG"}, None)

        driver._process_slots(
            {
                "print": {
                    "command": "push_status",
                    "ams": {
                        "ams": [
                            {
                                "id": "0",
                                "tray": [
                                    {
                                        "id": "0",
                                        "tray_type": "PETG",
                                        "tray_color": "ADB1B2FF",
                                        "tray_info_idx": "GFG02",
                                        "tray_uuid": "B94E172402D14E4AB97571340E303DBA",
                                    }
                                ],
                            }
                        ]
                    },
                }
            }
        )

        slot = driver._current_slots[0]
        self.assertEqual(slot["spool_id"], 17)
        self.assertEqual(driver._slot_spool_ids["0-0"], 17)
        driver._emit_slots_with_spool_ids()
        self.assertEqual(driver.health()["slots"][0]["spool_id"], 17)
        self.assertIsNone(driver._pending)

    def test_pending_spool_without_a_tray_uuid_still_lands_in_the_slot(self):
        """Third-party filament on a Bambu spool reports no readable tag."""
        driver, _ = make_driver(read_only=True)
        driver._current_slots = [
            {
                "slot_index": "0-0",
                "slot_name": "AMS 1 - Slot 1",
                "tray_type": "",
                "tray_color": "",
                "tray_info_idx": "",
                "present": False,
            }
        ]
        driver._pending = PendingSpool(99, {"material_type": "PLA"}, None)

        driver._process_slots(
            {
                "print": {
                    "command": "push_status",
                    "ams": {
                        "ams": [
                            {
                                "id": "0",
                                "tray": [
                                    {
                                        "id": "0",
                                        "tray_type": "PLA",
                                        "tray_color": "00FF00FF",
                                        "tray_info_idx": "GFL99",
                                    }
                                ],
                            }
                        ]
                    },
                }
            }
        )

        self.assertEqual(driver._current_slots[0]["spool_id"], 99)
        self.assertEqual(driver._slot_spool_ids["0-0"], 99)
        self.assertEqual(driver._spool_ids_by_tray_uuid, {})

    def test_tray_going_empty_schedules_the_location_release(self):
        driver, _ = make_driver(auto_import_spools=False)
        driver._current_slots = [
            {
                "slot_index": "0-0",
                "slot_name": "AMS 1 - Slot 1",
                "tray_type": "PLA",
                "tray_color": "FF0000FF",
                "tray_info_idx": "GFA00",
                "present": True,
            }
        ]
        released = []
        driver._schedule_slot_location_release = released.append

        driver._process_slots(
            {
                "print": {
                    "command": "push_status",
                    "ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}]},
                }
            }
        )

        self.assertEqual(released, ["0-0"])

    def test_empty_tray_emits_null_with_or_without_cached_identity(self):
        """A cold driver and a stale RFID cache must both clear the core link."""
        uuid = "A" * 32
        for cached in (False, True):
            with self.subTest(cached=cached):
                driver, events = make_driver()
                if cached:
                    driver._slot_spool_ids["0-0"] = 42
                    driver._spool_ids_by_tray_uuid[uuid] = 42
                driver._process_slots({"print": {"ams": {"ams": [{
                    "id": "0", "tray": [{"id": "0", "tray_type": "", "tray_uuid": uuid}],
                }]}}})
                self.assertIsNone(events[-1]["slots"][0]["spool_id"])
                self.assertNotIn("0-0", driver._slot_spool_ids)

    def test_reemit_clears_empty_slot_but_keeps_manual_non_bambu_assignment(self):
        """Re-emission must not restore a cached RFID ID on an empty slot."""
        driver, events = make_driver()
        uuid = "A" * 32
        driver._spool_ids_by_tray_uuid[uuid] = 42
        driver._slot_spool_ids = {"0-0": 42, "0-1": 99}
        driver._current_slots = [
            {"slot_index": "0-0", "present": False, "tray_uuid": uuid, "spool_id": 42},
            {"slot_index": "0-1", "present": True, "tray_uuid": "0" * 32, "spool_id": 99},
        ]
        driver._emit_slots_with_spool_ids()
        self.assertIsNone(events[-1]["slots"][0]["spool_id"])
        self.assertEqual(events[-1]["slots"][1]["spool_id"], 99)
        self.assertEqual(driver._slot_spool_ids, {"0-1": 99})

    def test_a_tray_that_stays_empty_is_not_released_again(self):
        """Otherwise every poll of an empty AMS hits the database."""
        driver, _ = make_driver(auto_import_spools=False)
        driver._current_slots = [
            {
                "slot_index": "0-0",
                "slot_name": "AMS 1 - Slot 1",
                "tray_type": "",
                "tray_color": "",
                "tray_info_idx": "",
                "present": False,
            }
        ]
        released = []
        driver._schedule_slot_location_release = released.append

        driver._process_slots(
            {
                "print": {
                    "command": "push_status",
                    "ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}]},
                }
            }
        )

        self.assertEqual(released, [])

    @staticmethod
    def _swap_driver(replacement_spool_id=None):
        """Build one occupied slot plus optional cached replacement UUID."""
        first_uuid = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        second_uuid = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
        driver, _ = make_driver(auto_import_spools=False)
        driver._current_slots = [
            {
                "slot_index": "0-0",
                "tray_type": "PLA",
                "tray_color": "FF0000FF",
                "tray_info_idx": "GFA00",
                "tray_uuid": first_uuid,
                "present": True,
                "spool_id": 17,
            }
        ]
        driver._slot_spool_ids = {"0-0": 17}
        driver._spool_ids_by_tray_uuid = {first_uuid: 17}
        if replacement_spool_id is not None:
            driver._spool_ids_by_tray_uuid[second_uuid] = replacement_spool_id
        driver._slot_display_metadata = {"0-0": {"shop_image_url": "old"}}
        released = []
        updated = []
        driver._schedule_slot_location_release = released.append
        driver._schedule_slot_location_update = (
            lambda *args: updated.append(args)
        )
        driver._process_slots(
            {
                "print": {
                    "ams": {
                        "ams": [
                            {
                                "id": "0",
                                "tray": [
                                    {
                                        "id": "0",
                                        "tray_type": "PLA",
                                        "tray_color": "FF0000FF",
                                        "tray_info_idx": "GFA00",
                                        "tray_uuid": second_uuid,
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        )
        return driver, released, updated

    def test_direct_unknown_spool_swap_clears_the_previous_assignment(self):
        """A present-to-present UUID change must not retain spool A for spool B."""
        driver, released, updated = self._swap_driver()

        self.assertEqual(driver._slot_spool_ids, {})
        self.assertIsNone(driver._current_slots[0]["spool_id"])
        self.assertNotIn("0-0", driver._slot_display_metadata)
        self.assertEqual(released, ["0-0"])
        self.assertEqual(updated, [])

    def test_direct_known_spool_swap_assigns_the_replacement(self):
        """A cached tray UUID can move spool B into the slot immediately."""
        driver, released, updated = self._swap_driver(replacement_spool_id=18)

        self.assertEqual(driver._slot_spool_ids, {"0-0": 18})
        self.assertEqual(driver._current_slots[0]["spool_id"], 18)
        self.assertEqual(released, [])
        self.assertEqual(updated[0][:2], ("0-0", 18))

    def test_external_location_has_human_slot_number(self):
        driver, _ = make_driver()
        driver._printer_name = "Test Printer"
        self.assertEqual(
            driver._generate_slot_location_name(255, 254),
            "Test Printer - ext. Slot 1",
        )
        self.assertEqual(
            driver._generate_slot_location_name(255, 255),
            "Test Printer - ext. Slot 2",
        )

    def test_processes_new_vir_slot_list_and_ams_ht_ids(self):
        driver, _ = make_driver(auto_import_spools=False)
        driver._process_slots(
            {
                "print": {
                    "ams": {
                        "ams": [
                            {
                                "id": "128",
                                "tray": [
                                    {"id": "0", "tray_type": "PLA"}
                                ],
                            }
                        ]
                    },
                    "vir_slot": [
                        {"id": "254", "tray_type": "PETG"},
                        {"id": "255", "tray_type": "TPU"},
                    ],
                }
            }
        )

        self.assertEqual(
            [slot["slot_index"] for slot in driver._current_slots],
            ["128-0", "255-254", "255-255"],
        )
        self.assertEqual(
            [slot["slot_name"] for slot in driver._current_slots],
            ["AMS HT 1 - Slot 1", "External Tray 1", "External Tray 2"],
        )
        ams_info = driver._build_ams_info(driver._current_slots)
        self.assertEqual(ams_info["slot_count"], 3)
        self.assertTrue(ams_info["external_spool"])

    def test_health_reports_the_ams_info_flags_of_each_unit(self):
        driver, _ = make_driver(auto_import_spools=False)
        driver._process_slots(
            {
                "print": {
                    "ams": {
                        "ams": [
                            {"id": "0", "info": "1003", "tray": [{"id": "0", "tray_type": "PLA"}]},
                            {"id": "128", "info": "1004", "tray": [{"id": "0", "tray_type": "PA"}]},
                        ]
                    }
                }
            }
        )
        self.assertEqual(
            [(unit["ams_id"], unit["info"]) for unit in driver.health()["ams_units"]],
            [(0, "1003"), (128, "1004")],
        )

        # A later report without the flags must not forget them.
        driver._process_slots(
            {
                "print": {
                    "ams": {
                        "ams": [
                            {"id": "0", "humidity": "3", "tray": [{"id": "0", "tray_type": "PLA"}]},
                            {"id": "128", "tray": [{"id": "0", "tray_type": "PA"}]},
                        ]
                    }
                }
            }
        )
        self.assertEqual(
            [unit["info"] for unit in driver.health()["ams_units"]],
            ["1003", "1004"],
        )

    def test_empty_vir_slot_list_falls_back_to_vt_tray(self):
        driver, _ = make_driver(auto_import_spools=False)
        driver._process_slots(
            {
                "print": {
                    "vir_slot": [],
                    "vt_tray": {"id": "254", "tray_type": "PETG"},
                }
            }
        )

        self.assertEqual(
            [slot["slot_index"] for slot in driver._current_slots], ["255-254"]
        )
        self.assertEqual(driver._current_slots[0]["tray_type"], "PETG")

    def test_empty_vir_slot_list_keeps_previous_external_slots(self):
        driver, _ = make_driver(auto_import_spools=False)
        driver._process_slots(
            {"print": {"vir_slot": [{"id": "254", "tray_type": "PETG"}]}}
        )
        self.assertEqual(
            [slot["slot_index"] for slot in driver._current_slots], ["255-254"]
        )

        # A push_status with an empty vir_slot list and no vt_tray must not
        # wipe the previously known external tray (no spurious slots_update
        # with the external spool suddenly gone).
        driver._process_slots({"print": {"vir_slot": []}})

        self.assertEqual(
            [slot["slot_index"] for slot in driver._current_slots], ["255-254"]
        )
        self.assertEqual(driver._current_slots[0]["tray_type"], "PETG")


class FilamentSettingTests(unittest.TestCase):
    def setUp(self):
        from bambulabs_api import Printer as BambuPrinter
        from bambulabs_api import PrinterMQTTClient

        self.driver, _ = make_driver(serial="TEST-SERIAL")
        self.enterContext(patch.object(DRIVER_MODULE, "FILAMENT_SETTING_TIMEOUT", 0))
        self.client = Mock()
        self.client.is_connected.return_value = True
        self.client.publish.return_value.is_published.return_value = True
        mqtt = PrinterMQTTClient.__new__(PrinterMQTTClient)
        mqtt._client = self.client
        mqtt.command_topic = "device/TEST-SERIAL/request"
        printer = BambuPrinter.__new__(BambuPrinter)
        printer.mqtt_client = mqtt
        self.driver._printer = printer

    def test_filament_settings_use_distinct_decimal_string_sequences(self):
        self.driver._do_send_filament_setting(129, 0, {})
        self.driver._do_send_filament_setting(129, 1, {})

        sequences = [
            json.loads(call.args[1])["print"]["sequence_id"]
            for call in self.client.publish.call_args_list
        ]
        self.assertTrue(all(isinstance(seq, str) and seq.isdecimal() for seq in sequences))
        self.assertEqual(len(set(sequences)), 2)

    def test_debug_distinguishes_printer_ack_from_mqtt_publish(self):
        from bambulabs_api import PrinterMQTTClient

        printer = self.driver._printer
        mqtt = PrinterMQTTClient("localhost", "test", "TEST-SERIAL")
        mqtt._client.publish = self.client.publish
        mqtt._client.is_connected = self.client.is_connected
        printer.mqtt_client = mqtt
        printer.mqtt_start = Mock()
        self.driver._load_printer_name = AsyncMock(return_value="Test Printer")
        self.driver._load_saved_ams_slots = AsyncMock(return_value={})
        self.driver._ensure_shared_extra_fields = AsyncMock()
        with patch("bambulabs_api.Printer", return_value=printer):
            asyncio.run(self.driver.start())

        for result, nested, published, expected_ack in (
            ("SUCCESS", True, True, "SUCCESS"),
            ("FAIL", False, True, "FAIL"),
            (None, True, True, "timeout"),
            (None, True, False, "not_published"),
        ):
            with self.subTest(ack=expected_ack):
                self.driver.clear_debug_log()
                self.driver.enable_debug_log()
                self.client.publish.return_value.is_published.return_value = published

                def publish(topic, payload):
                    sequence = json.loads(payload)["print"]["sequence_id"]
                    for seq, ack_result in (("0", "SUCCESS"), (sequence, result)):
                        if ack_result is None:
                            continue
                        report = {
                            "command": "ams_filament_setting", "sequence_id": seq,
                            "result": ack_result,
                        }
                        if ack_result == "FAIL":
                            report["reason"] = "ERROR COMMAND"
                        message = Mock(
                            topic="device/TEST-SERIAL/report",
                            payload=json.dumps({"print": report} if nested else report).encode(),
                        )
                        mqtt._client.on_message(mqtt._client, None, message)
                    return self.client.publish.return_value

                self.client.publish.side_effect = publish
                self.driver._do_send_filament_setting(129, 0, {})

                entry = self.driver.get_debug_log()[-1]["payload"]
                self.assertEqual(entry["mqtt_published"], published)
                self.assertEqual(entry["printer_ack"], expected_ack)
                self.assertEqual(entry["printer_reason"], "ERROR COMMAND" if result == "FAIL" else "")
                self.assertEqual(entry["success"], result == "SUCCESS")
                self.assertFalse(self.driver._filament_acks)

    def test_sends_custom_profile_and_setting_id_in_one_mqtt_command(self):
        self.driver._do_send_filament_setting(129, 0, {
            "tray_info_idx": "P618a7ac", "setting_id": "PFUSed1e487ad8098f",
            "nozzle_temp_min": 250, "nozzle_temp_max": 270,
            "material_type": "ASA+", "color": "aabbcc80",
        })

        self.assertEqual(self.client.publish.call_count, 1)
        topic, payload = self.client.publish.call_args.args
        self.assertEqual(topic, "device/TEST-SERIAL/request")
        settings = json.loads(payload)["print"]
        settings.pop("sequence_id")
        self.assertEqual(settings, {
            "command": "ams_filament_setting", "ams_id": 129, "tray_id": 0,
            "tray_info_idx": "P618a7ac", "setting_id": "PFUSed1e487ad8098f",
            "nozzle_temp_min": 250, "nozzle_temp_max": 270,
            "tray_type": "ASA+", "tray_color": "AABBCCFF",
        })
        self.client.publish.return_value.wait_for_publish.assert_called_once_with(timeout=0)

    def test_omits_empty_setting_id_and_preserves_external_slot_mapping(self):
        for setting_id in (None, ""):
            with self.subTest(setting_id=setting_id):
                self.client.reset_mock()
                self.driver._do_send_filament_setting(200, 0, {"setting_id": setting_id})

                self.assertEqual(self.client.publish.call_count, 1)
                payload = json.loads(self.client.publish.call_args.args[1])["print"]
                payload.pop("sequence_id")
                self.assertEqual(payload, {
                    "command": "ams_filament_setting", "ams_id": 255, "tray_id": 254,
                    "tray_info_idx": "GFL99", "nozzle_temp_min": 190,
                    "nozzle_temp_max": 230, "tray_type": "PLA", "tray_color": "FFFFFFFF",
                })

    def test_second_external_tray_uses_the_second_wire_address(self):
        self.driver._do_send_filament_setting(255, 1, {})
        payload = json.loads(self.client.publish.call_args.args[1])["print"]
        self.assertEqual((payload["ams_id"], payload["tray_id"]), (255, 255))

    def test_disconnected_or_read_only_never_publishes(self):
        for read_only, connected in ((True, True), (False, False)):
            with self.subTest(read_only=read_only, connected=connected):
                self.driver._read_only = read_only
                self.client.is_connected.return_value = connected
                self.driver._do_send_filament_setting(129, 0, {"setting_id": "PFUS123"})
                self.client.publish.assert_not_called()

    def test_publish_error_is_contained(self):
        self.client.publish.side_effect = RuntimeError("MQTT unavailable")
        with self.assertLogs(DRIVER_MODULE.logger, level="ERROR") as logs:
            self.driver._do_send_filament_setting(129, 0, {})
        self.assertIn("MQTT unavailable", logs.output[0])


class ReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_assignment_in_read_only_records_spool_without_mqtt(self):
        """Manual non-RFID assignment must work with each supported ID handoff."""
        for data, explicit_id in (({}, 42), ({"id": 42}, None), ({"filaman_spool_id": 42}, None)):
            with self.subTest(data=data, explicit_id=explicit_id):
                driver, events = make_driver(read_only=True)
                driver._current_slots = [{"slot_index": "0-0", "present": True}]
                driver._update_spool_location = AsyncMock()
                with patch.object(DRIVER_MODULE.threading, "Thread") as mqtt_thread:
                    await driver.send_filament_to_tray(0, 0, data, spool_id=explicit_id)
                    mqtt_thread.assert_not_called()
                driver._update_spool_location.assert_awaited_once_with(42, 0, 0)
                self.assertEqual(driver._slot_spool_ids["0-0"], 42)
                self.assertEqual(events[-1]["slots"][0]["spool_id"], 42)
                # A later non-RFID MQTT snapshot must retain the manual mapping.
                driver._process_slots({"print": {"ams": {"ams": [{
                    "id": "0", "tray": [{"id": "0", "tray_type": "PLA",
                                           "tray_color": "FF0000FF"}],
                }]}}})
                self.assertEqual(driver._current_slots[0]["spool_id"], 42)

    async def test_read_only_blocks_direct_setting(self):
        driver, _ = make_driver(read_only=True)
        driver._printer = object()
        dispatched = driver._send_filament_setting(0, 0, {"material_type": "PLA"})
        self.assertFalse(dispatched)

    async def test_reconnect_reports_removal_from_mask_without_repeating_trays(self):
        driver, events = make_driver(printer_model="P2S")
        driver._loop = asyncio.get_running_loop()
        driver._schedule_slot_location_release = Mock()

        def report(ams):
            message = Mock(topic="device/serial/report")
            message.payload = json.dumps({"print": {"command": "push_status", "ams": ams}}).encode()
            driver._on_message(None, None, message)

        driver._on_connect(None, None, None, None, 0, None)
        report({
            "tray_exist_bits": "1",
            "ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]}],
        })
        await asyncio.sleep(0)
        self.assertTrue(events[-1]["slots"][0]["present"])

        report({"tray_exist_bits": "0", "power_on_flag": False})
        driver._on_disconnect(None, None, None, None, 0, None)
        driver._on_connect(None, None, None, None, 0, None)
        report({"tray_exist_bits": "0", "power_on_flag": True})
        await asyncio.sleep(0)

        self.assertFalse(events[-1]["slots"][0]["present"])
        self.assertIsNone(events[-1]["slots"][0]["spool_id"])
        driver._schedule_slot_location_release.assert_called_once_with("0-0")

    async def test_direct_assignment_accepts_canonical_and_wire_external_addresses(self):
        driver, _ = make_driver(read_only=True)
        driver._process_slots({"print": {"vir_slot": [
            {"id": "254", "tray_type": "PLA"}, {"id": "255", "tray_type": "PETG"},
        ]}})
        driver._update_spool_location = AsyncMock()
        for canonical, wire, spool_id in ((0, 254, 17), (1, 255, 18), (255, 255, 19)):
            await driver.send_filament_to_tray(255, canonical, {}, spool_id=spool_id)
            driver._update_spool_location.assert_awaited_with(spool_id, 255, wire)
        self.assertEqual([slot["spool_id"] for slot in driver.health()["slots"]], [17, 19])

    async def test_external_alias_reports_match_targeted_pending_and_wire_addresses(self):
        driver, _ = make_driver(read_only=True)
        driver._process_slots({"print": {"vir_slot": [{"id": "1", "tray_type": ""}]}})
        await driver.assign_pending_spool(42, {"material_type": "PLA"}, slot_index="255-1")
        driver._process_slots({"print": {"vir_slot": [{"id": "255", "tray_type": "PLA"}]}})
        self.assertEqual(driver.health()["slots"][0]["slot_index"], "255-255")
        self.assertEqual(driver.health()["slots"][0]["spool_id"], 42)

    async def test_direct_link_to_confirmed_empty_slot_cannot_move_inventory(self):
        driver, _ = make_driver(read_only=True)
        driver._update_spool_location = AsyncMock()
        driver._process_slots({"print": {"ams": {
            "tray_exist_bits": "0", "ams": [{"id": "0", "tray": [{"id": "0"}]}],
        }}})
        with self.assertRaisesRegex(TypeError, "empty"):
            await driver.send_filament_to_tray(0, 0, {}, spool_id=42)
        driver._update_spool_location.assert_not_awaited()
        self.assertIsNone(driver.health()["slots"][0]["spool_id"])

    async def test_direct_link_to_unverified_external_waits_for_feedback(self):
        driver, _ = make_driver()
        driver._update_spool_location = AsyncMock()
        driver._schedule_slot_location_update = Mock()
        driver._send_filament_setting = Mock()
        driver._process_slots({"print": {"vir_slot": [{"id": "255", "tray_type": ""}]}})
        filament = {"material_type": "PLA"}

        await driver.send_filament_to_tray(255, 1, filament, spool_id=42)
        driver._update_spool_location.assert_not_awaited()
        self.assertIsNone(driver.health()["slots"][0]["spool_id"])

        driver._process_slots({"print": {"vir_slot": [{"id": "255", "tray_type": "PLA"}]}})
        self.assertEqual(driver.health()["slots"][0]["spool_id"], 42)
        driver._schedule_slot_location_update.assert_called_once_with("255-255", 42)
        driver._send_filament_setting.assert_called_once_with(255, 255, filament)

    async def test_read_only_direct_link_cannot_verify_blank_external(self):
        driver, _ = make_driver(read_only=True)
        driver._update_spool_location = AsyncMock()
        driver._process_slots({"print": {"vt_tray": {"id": "254", "tray_type": ""}}})
        with self.assertRaisesRegex(TypeError, "read-only"):
            await driver.send_filament_to_tray(255, 0, {}, spool_id=42)
        driver._update_spool_location.assert_not_awaited()

    async def test_failed_claim_drops_pending_before_sending_or_linking(self):
        driver, _ = make_driver()
        driver._send_filament_setting = Mock()
        driver._schedule_slot_location_update = Mock()
        driver._process_slots({"print": {"ams": {
            "tray_exist_bits": "0", "ams": [{"id": "0", "tray": [{"id": "0"}]}],
        }}})
        driver._schedule_slot_location_update.reset_mock()
        claim = Mock(return_value=False)
        await driver.assign_pending_spool(42, {"material_type": "PLA"}, claim=claim)
        driver._process_slots({"print": {"ams": {"tray_exist_bits": "1"}}})
        self.assertFalse(driver.health()["pending"])
        self.assertIsNone(driver.health()["slots"][0].get("spool_id"))
        claim.assert_called_once_with()
        driver._send_filament_setting.assert_not_called()
        driver._schedule_slot_location_update.assert_not_called()

    async def test_read_only_keeps_the_assignment_but_sends_nothing(self):
        """Read-only is about the printer, not about FilaMan's own records."""
        driver, _ = make_driver(read_only=True)
        await driver.assign_pending_spool(42, {"material_type": "PLA"})
        self.assertIsNotNone(driver._pending)
        self.assertEqual(driver._pending.spool_id, 42)
        if driver._pending.timer:
            driver._pending.timer.cancel()


class AutoImportDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.original_session_makers = {
            DRIVER_MODULE: DRIVER_MODULE.async_session_maker,
            CATALOG_MODULE: CATALOG_MODULE.async_session_maker,
            ENRICHMENT_MODULE: ENRICHMENT_MODULE.async_session_maker,
            SPOOL_SYNC_MODULE: SPOOL_SYNC_MODULE.async_session_maker,
        }
        for module in self.original_session_makers:
            module.async_session_maker = self.sessions

        # The per-filament shop-image lock/throttle are class-level (shared
        # across Driver instances by design, see CatalogMixin), so previous
        # tests' asyncio.Lock objects (bound to an already-closed event loop)
        # must not leak into this test's fresh loop.
        CATALOG_MODULE.CatalogMixin._shop_image_locks.clear()
        CATALOG_MODULE.CatalogMixin._shop_image_last_attempt.clear()

        self.driver, self.events = make_driver(
            auto_import_spools=True,
            sync_spool_weight=True,
        )
        self.driver._loop = asyncio.get_running_loop()
        self.driver._auto_import_lock = asyncio.Lock()
        self.driver._printer_name = "Test Printer"

        async def no_store_search(_product_code, _product_url=None):
            return None

        self.driver._fetch_store_search_image = no_store_search
        self.slot = {
            "slot_index": "0-0",
            "slot_name": "AMS 1 - Slot 1",
            "tray_type": "PLA",
            "tray_color": "FF0000FF",
            "tray_info_idx": "GFA00",
            "tag_uid": "A1B2C3D4E5F60102",
            "tray_uuid": "AABBCCDDEEFF0011AABBCCDDEEFF0011",
            "tray_weight": "1000",
            "remain": 75,
            "present": True,
        }
        self.driver._current_slots = [dict(self.slot)]

        async with self.sessions() as db:
            manufacturer = Manufacturer(name="Bambu Lab")
            color = Color(name="Bambu Red", hex_code="#FF0000")
            db.add_all(
                [
                    manufacturer,
                    color,
                    Printer(
                        id=1,
                        name="Test Printer",
                        driver_key="bambulab",
                        driver_config={},
                    ),
                    SpoolStatus(key="new", label="New", is_system=True),
                    SpoolStatus(key="opened", label="Opened", is_system=True),
                ]
            )
            await db.flush()
            filament = Filament(
                manufacturer_id=manufacturer.id,
                designation="Bambu PLA Basic",
                material_type="PLA",
                diameter_mm=1.75,
            )
            db.add(filament)
            await db.flush()
            self.filament_id = filament.id
            db.add_all(
                [
                    FilamentColor(
                        filament_id=filament.id,
                        color_id=color.id,
                        position=1,
                    ),
                    FilamentPrinterParam(
                        filament_id=filament.id,
                        printer_id=1,
                        param_key="bambu_tray_idx",
                        param_value="GFA00",
                    ),
                ]
            )
            await db.commit()

    async def asyncTearDown(self):
        for module, session_maker in self.original_session_makers.items():
            module.async_session_maker = session_maker
        await self.engine.dispose()

    async def test_shared_article_number_and_rfid_fields_are_visible_without_duplicates(self):
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.custom_fields = {"article_number": "13109"}
            opened_status_id = await db.scalar(
                select(SpoolStatus.id).where(SpoolStatus.key == "opened")
            )
            db.add(
                SystemExtraField(
                    target_type="filament",
                    key="article_number",
                    label="Bambu Color Code (Article Number)",
                    field_type="text",
                    source="filascan_import",
                    config={"max_length": 5},
                )
            )
            db.add(
                Spool(
                    filament_id=self.filament_id,
                    status_id=opened_status_id,
                    custom_fields={"article_number": "13109"},
                )
            )
            await db.commit()

        await self.driver._ensure_shared_extra_fields()

        async with self.sessions() as db:
            fields = list(
                (
                    await db.execute(
                        select(SystemExtraField).order_by(
                            SystemExtraField.target_type,
                            SystemExtraField.key,
                        )
                    )
                ).scalars()
            )
        self.assertEqual(
            [
                (field.target_type, field.key, field.source, field.config)
                for field in fields
            ],
            [
                (
                    "filament",
                    "article_number",
                    "filascan_import",
                    {"max_length": 5},
                ),
                (
                    "spool",
                    "bambu_rfid_tag_1",
                    "bambulab",
                    {"max_length": 32},
                ),
                (
                    "spool",
                    "bambu_rfid_tag_2",
                    "bambulab",
                    {"max_length": 32},
                ),
            ],
        )
        async with self.sessions() as db:
            spool = (await db.execute(select(Spool))).scalar_one()
        self.assertEqual(spool.custom_fields["article_number"], "13109")

    async def test_import_is_idempotent_and_keeps_custom_rfid_free(self):
        await self.driver._auto_import_rfid_spools([self.slot])
        await self.driver._auto_import_rfid_spools(
            [{**self.slot, "tag_uid": "1020304050607080"}]
        )

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(count, 1)
        self.assertEqual(
            spool.external_id,
            "bambulab:AABBCCDDEEFF0011AABBCCDDEEFF0011",
        )
        self.assertEqual(
            spool.custom_fields[SPOOL_SYNC_MODULE.BAMBU_RFID_TAG_1_FIELD],
            "A1B2C3D4E5F60102",
        )
        self.assertNotIn(
            SPOOL_SYNC_MODULE.BAMBU_RFID_TAG_2_FIELD, spool.custom_fields
        )
        self.assertIsNone(spool.rfid_uid)
        self.assertEqual(spool.remaining_weight_g, 750)

    async def test_import_reuses_spool_with_tray_uuid_in_rfid_uid(self):
        legacy_rfid_uid = (
            "aa:bb:cc:dd:ee:ff:00:11:aa:bb:cc:dd:ee:ff:00:11"
        )
        async with self.sessions() as db:
            status_id = await db.scalar(
                select(SpoolStatus.id).where(SpoolStatus.key == "opened")
            )
            legacy_spool = Spool(
                filament_id=self.filament_id,
                status_id=status_id,
                rfid_uid=legacy_rfid_uid,
                remaining_weight_g=1000,
                custom_fields={},
            )
            db.add(legacy_spool)
            await db.commit()
            await db.refresh(legacy_spool)
            legacy_spool_id = legacy_spool.id

        await self.driver._auto_import_rfid_spools([self.slot])

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(count, 1)
        self.assertEqual(spool.id, legacy_spool_id)
        self.assertEqual(spool.rfid_uid, legacy_rfid_uid)
        self.assertEqual(
            spool.external_id,
            "bambulab:AABBCCDDEEFF0011AABBCCDDEEFF0011",
        )
        self.assertEqual(spool.remaining_weight_g, 750)
        self.assertEqual(
            spool.custom_fields[SPOOL_SYNC_MODULE.BAMBU_RFID_TAG_1_FIELD],
            "A1B2C3D4E5F60102",
        )

    async def _spool_carrying(self, custom_fields):
        """A spool that already exists, holding the tray uuid where given."""
        async with self.sessions() as db:
            status_id = await db.scalar(
                select(SpoolStatus.id).where(SpoolStatus.key == "opened")
            )
            spool = Spool(
                filament_id=self.filament_id,
                status_id=status_id,
                remaining_weight_g=1000,
                custom_fields=custom_fields,
            )
            db.add(spool)
            await db.commit()
            await db.refresh(spool)
            return spool.id

    async def test_import_reuses_spool_with_tray_uuid_in_custom_fields(self):
        """The Spoolman import writes the tray uuid into ``tag``."""
        existing_id = await self._spool_carrying(
            {"spoolman_id": 274, "tag": "AABBCCDDEEFF0011AABBCCDDEEFF0011"}
        )

        await self.driver._auto_import_rfid_spools([self.slot])

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(count, 1)
        self.assertEqual(spool.id, existing_id)
        self.assertEqual(
            spool.external_id,
            "bambulab:AABBCCDDEEFF0011AABBCCDDEEFF0011",
        )

    async def test_import_reuses_spool_with_tray_uuid_in_spoolman_extra(self):
        """Older imports nest it under ``spoolman_extra``, and may separate it."""
        existing_id = await self._spool_carrying(
            {
                "spoolman_id": "33",
                "spoolman_extra": {
                    "tag": "aa:bb:cc:dd:ee:ff:00:11:aa:bb:cc:dd:ee:ff:00:11",
                    "german_name": "Schwarz",
                },
            }
        )

        await self.driver._auto_import_rfid_spools([self.slot])

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(count, 1)
        self.assertEqual(spool.id, existing_id)

    async def test_import_checks_both_spoolman_tag_locations(self):
        """An unrelated top-level tag must not hide a matching nested tag."""
        existing_id = await self._spool_carrying(
            {
                "tag": "11111111111111112222222222222222",
                "spoolman_extra": {
                    "tag": "AABBCCDDEEFF0011AABBCCDDEEFF0011"
                },
            }
        )

        await self.driver._auto_import_rfid_spools([self.slot])

        async with self.sessions() as db:
            spools = list((await db.execute(select(Spool))).scalars())

        self.assertEqual([spool.id for spool in spools], [existing_id])

    async def test_import_ignores_custom_fields_of_a_different_spool(self):
        """A tag that belongs to another tray must not swallow this import."""
        await self._spool_carrying(
            {"tag": "11111111111111112222222222222222"}
        )

        await self.driver._auto_import_rfid_spools([self.slot])

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            imported = await db.scalar(
                select(Spool).where(
                    Spool.external_id
                    == "bambulab:AABBCCDDEEFF0011AABBCCDDEEFF0011"
                )
            )

        self.assertEqual(count, 2)
        self.assertIsNotNone(imported)

    async def test_assigning_a_tray_clears_the_spool_that_was_there(self):
        """A tray holds one spool, so the previous one has to let go."""
        first = await self._spool_carrying({})
        second = await self._spool_carrying({})

        await self.driver._update_spool_location(first, 0, 0)
        await self.driver._update_spool_location(second, 0, 0)

        async with self.sessions() as db:
            location = await db.scalar(
                select(Location).where(
                    Location.name == "Test Printer - AMS A1"
                )
            )
            left = await db.get(Spool, first)
            arrived = await db.get(Spool, second)

        self.assertIsNone(left.location_id)
        self.assertEqual(arrived.location_id, location.id)

    async def test_emptying_a_tray_takes_the_spool_out_of_the_location(self):
        """An empty tray says nothing about where its spool went."""
        spool_id = await self._spool_carrying({})
        await self.driver._update_spool_location(spool_id, 0, 0)

        await self.driver._release_slot_location(0, 0)

        async with self.sessions() as db:
            spool = await db.get(Spool, spool_id)
        self.assertIsNone(spool.location_id)

    async def test_initial_topology_retires_a_saved_ams_slot_without_inventing_live_slots(self):
        spool_id = await self._spool_carrying({})
        await self.driver._update_spool_location(spool_id, 128, 0)
        async with self.sessions() as db:
            db.add_all([
                PrinterSlot(
                    printer_id=1, slot_no=number, name=name, is_active=active,
                    custom_fields={"slot_index": index},
                )
                for number, name, index, active in (
                    (1, "Still connected", "0-1", True),
                    (2, "Detached AMS HT", "128-0", True),
                    (3, "External", "255-254", True),
                    (4, "Tool", "16-0", True),
                    (5, "Already inactive", "1-3", False),
                )
            ])
            await db.commit()

        fresh, events = make_driver()
        fresh._ensure_shared_extra_fields = AsyncMock()
        with patch("bambulabs_api.Printer"):
            await fresh.start()
        self.assertEqual(fresh.health()["slots"], [])
        self.assertEqual(events, [])

        status = {"print": {"ams": {"ams_exist_bits": "1"}}}
        fresh._process_slots(status)
        fresh._process_slots(status)
        await asyncio.sleep(0)
        await asyncio.gather(*(asyncio.all_tasks() - {asyncio.current_task()}))

        self.assertEqual(fresh.health()["slots"], [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["slots"], [{
            "slot_index": "128-0", "slot_name": "Detached AMS HT",
            "present": False, "is_active": False, "spool_id": None,
        }])
        async with self.sessions() as db:
            spool = await db.get(Spool, spool_id)
            self.assertIsNone(spool.location_id)

    async def test_first_empty_report_releases_a_persisted_managed_location(self):
        spool_id = await self._spool_carrying({})
        await self.driver._update_spool_location(spool_id, 0, 0)
        fresh, _ = make_driver()
        fresh._loop = asyncio.get_running_loop()
        fresh._process_slots({"print": {"ams": {
            "tray_exist_bits": "0", "ams": [{"id": "0", "tray": [{"id": "0"}]}],
        }}})
        # Join work submitted by the synchronous MQTT status handler.
        await asyncio.sleep(0)
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        if pending:
            await asyncio.gather(*pending)
        async with self.sessions() as db:
            spool = await db.get(Spool, spool_id)
            self.assertIsNone(spool.location_id)

    async def test_delayed_rfid_lookup_cannot_relink_a_removed_tray(self):
        spool_id = await self._spool_carrying({"tag": self.slot["tray_uuid"]})
        self.driver._schedule_auto_import = Mock()
        original_find = self.driver._find_existing_bambu_spool

        async def find_after_removal(*args):
            self.driver._process_slots({"print": {"ams": {"tray_exist_bits": "0"}}})
            return await original_find(*args)

        self.driver._find_existing_bambu_spool = find_after_removal
        await self.driver._auto_import_rfid_spools([self.slot])
        await asyncio.sleep(0)
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        if pending:
            await asyncio.gather(*pending)
        self.assertIsNone(self.driver.health()["slots"][0]["spool_id"])
        async with self.sessions() as db:
            spool = await db.get(Spool, spool_id)
            self.assertIsNone(spool.location_id)

    async def test_inflight_release_cannot_undo_a_new_slot_assignment(self):
        old_spool = await self._spool_carrying({})
        new_spool = await self._spool_carrying({})
        await self.driver._update_spool_location(old_spool, 0, 0)
        self.driver._schedule_auto_import = Mock()

        release_started = asyncio.Event()
        finish_release = asyncio.Event()
        update_started = asyncio.Event()
        original_release = self.driver._release_slot_location
        original_update = self.driver._update_spool_location

        async def paused_release(*args):
            release_started.set()
            await finish_release.wait()
            await original_release(*args)

        async def update(*args):
            update_started.set()
            await original_update(*args)

        self.driver._release_slot_location = paused_release
        self.driver._update_spool_location = update
        self.driver._process_slots({"print": {"ams": {"tray_exist_bits": "0"}}})
        await asyncio.wait_for(release_started.wait(), timeout=1)

        self.driver._pending = PendingSpool(new_spool, {"material_type": "PLA"})
        self.driver._send_filament_setting = Mock()
        self.driver._process_slots({"print": {"ams": {"tray_exist_bits": "1"}}})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        update_ran_early = update_started.is_set()
        finish_release.set()

        await asyncio.gather(*(asyncio.all_tasks() - {asyncio.current_task()}))
        self.assertFalse(update_ran_early)
        async with self.sessions() as db:
            location = await db.scalar(select(Location).where(Location.identifier == "bambulab_1_0_0"))
            self.assertIsNone((await db.get(Spool, old_spool)).location_id)
            self.assertEqual((await db.get(Spool, new_spool)).location_id, location.id)

    async def test_inflight_rfid_cannot_replace_an_explicit_pending_assignment(self):
        uuid = self.slot["tray_uuid"]
        rfid_spool = await self._spool_carrying({"tag": uuid})
        assigned_spool = await self._spool_carrying({})
        self.driver._schedule_auto_import = Mock()
        self.driver._send_filament_setting = Mock()
        original_find = self.driver._find_existing_bambu_spool

        async def find_after_assignment(*args):
            self.driver._process_slots({"print": {"ams": {"tray_exist_bits": "0"}}})
            self.driver._pending = PendingSpool(assigned_spool, {"material_type": "PLA"})
            self.driver._process_slots({"print": {"ams": {"tray_exist_bits": "1"}}})
            self.driver._process_slots({"print": {"ams": {"ams": [
                {"id": "0", "tray": [{"id": "0", "tray_uuid": uuid, "tray_type": "PLA"}]},
            ]}}})
            return await original_find(*args)

        self.driver._find_existing_bambu_spool = find_after_assignment
        await self.driver._auto_import_rfid_spools([self.slot])
        await asyncio.sleep(0)
        await asyncio.gather(*(asyncio.all_tasks() - {asyncio.current_task()}))
        self.assertEqual(self.driver.health()["slots"][0]["spool_id"], assigned_spool)
        async with self.sessions() as db:
            self.assertIsNone((await db.get(Spool, rfid_spool)).location_id)
            self.assertIsNotNone((await db.get(Spool, assigned_spool)).location_id)

    async def test_late_cached_rfid_after_reinsertion_restores_the_managed_location(self):
        uuid = self.slot["tray_uuid"]
        spool_id = await self._spool_carrying({"tag": uuid})
        self.driver._schedule_auto_import = Mock()
        await self.driver._auto_import_rfid_spools([self.slot])
        self.driver._process_slots({"print": {"ams": {"tray_exist_bits": "0"}}})
        await asyncio.sleep(0)
        await asyncio.gather(*(asyncio.all_tasks() - {asyncio.current_task()}))
        self.driver._process_slots({"print": {"ams": {"tray_exist_bits": "1"}}})
        self.driver._process_slots({"print": {"ams": {"ams": [
            {"id": "0", "tray": [{"id": "0", "tray_uuid": uuid, "tray_type": "PLA"}]},
        ]}}})
        await asyncio.sleep(0)
        await asyncio.gather(*(asyncio.all_tasks() - {asyncio.current_task()}))

        self.assertEqual(self.driver.health()["slots"][0]["spool_id"], spool_id)
        async with self.sessions() as db:
            spool = await db.get(Spool, spool_id)
            self.assertIsNotNone(spool.location_id)

    async def test_removal_during_filament_lookup_prevents_stale_rfid_import(self):
        self.driver._schedule_auto_import = Mock()
        original_match = self.driver._find_matching_filament

        async def find_after_removal(*args):
            self.driver._process_slots({"print": {"ams": {"tray_exist_bits": "0"}}})
            return await original_match(*args)

        self.driver._find_matching_filament = find_after_removal
        await self.driver._auto_import_rfid_spools([self.slot])
        await asyncio.sleep(0)
        await asyncio.gather(*(asyncio.all_tasks() - {asyncio.current_task()}))
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Spool)), 0)

    async def test_printer_rename_reuses_the_stable_slot_location(self):
        """A display-name change must not create a second physical slot."""
        spool_id = await self._spool_carrying({})
        self.driver._printer_name = "Old Printer Name"
        await self.driver._update_spool_location(spool_id, 0, 0)

        self.driver._printer_name = "New Printer Name"
        await self.driver._update_spool_location(spool_id, 0, 0)

        async with self.sessions() as db:
            locations = list((await db.execute(select(Location))).scalars())

        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0].identifier, "bambulab_1_0_0")
        self.assertEqual(locations[0].name, "New Printer Name - AMS A1")

    async def test_equally_named_printers_get_distinct_slot_locations(self):
        """The human-readable printer name is not a database identity."""
        first_spool = await self._spool_carrying({})
        second_spool = await self._spool_carrying({})
        self.driver._printer_name = "P1S"
        second_driver, _ = make_driver(printer_id=2)
        second_driver._printer_name = "P1S"

        await self.driver._update_spool_location(first_spool, 0, 0)
        await second_driver._update_spool_location(second_spool, 0, 0)

        async with self.sessions() as db:
            locations = list(
                (
                    await db.execute(select(Location).order_by(Location.identifier))
                ).scalars()
            )
            first = await db.get(Spool, first_spool)
            second = await db.get(Spool, second_spool)

        self.assertEqual(
            [location.identifier for location in locations],
            ["bambulab_1_0_0", "bambulab_2_0_0"],
        )
        self.assertEqual(
            [location.name for location in locations],
            ["P1S - AMS A1", "P1S - AMS A1 [Printer 2]"],
        )
        self.assertNotEqual(first.location_id, second.location_id)

    async def test_adopts_an_identifierless_owned_legacy_location(self):
        """Plugin ownership plus printer and slot metadata is safe to migrate."""
        spool_id = await self._spool_carrying({})
        async with self.sessions() as db:
            legacy = Location(
                name="Previous Printer Name - AMS A1",
                custom_fields={
                    "managed_by": "bambulab_plugin",
                    "printer_id": 1,
                },
            )
            db.add(legacy)
            await db.commit()
            await db.refresh(legacy)
            legacy_id = legacy.id

        await self.driver._update_spool_location(spool_id, 0, 0)

        async with self.sessions() as db:
            locations = list((await db.execute(select(Location))).scalars())

        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0].id, legacy_id)
        self.assertEqual(locations[0].identifier, "bambulab_1_0_0")
        self.assertEqual(locations[0].name, "Test Printer - AMS A1")
        self.assertEqual(locations[0].custom_fields["printer_id"], 1)

    async def test_adopts_the_unambiguous_old_fallback_name(self):
        """The exact historical Printer <id> name is a conservative fallback."""
        spool_id = await self._spool_carrying({})
        async with self.sessions() as db:
            legacy = Location(name="Printer 1 - AMS A1", custom_fields={})
            db.add(legacy)
            await db.commit()
            await db.refresh(legacy)
            legacy_id = legacy.id

        await self.driver._update_spool_location(spool_id, 0, 0)

        async with self.sessions() as db:
            locations = list((await db.execute(select(Location))).scalars())

        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0].id, legacy_id)
        self.assertEqual(locations[0].identifier, "bambulab_1_0_0")
        self.assertEqual(
            locations[0].custom_fields["managed_by"], "bambulab_plugin"
        )

    async def test_does_not_adopt_an_unowned_manual_location(self):
        """A matching visible name alone is not permission to take ownership."""
        spool_id = await self._spool_carrying({})
        async with self.sessions() as db:
            manual = Location(name="Test Printer - AMS A1", custom_fields={})
            db.add(manual)
            await db.commit()
            await db.refresh(manual)
            manual_id = manual.id

        await self.driver._update_spool_location(spool_id, 0, 0)

        async with self.sessions() as db:
            locations = list(
                (await db.execute(select(Location).order_by(Location.id))).scalars()
            )

        self.assertEqual(len(locations), 2)
        self.assertEqual(locations[0].id, manual_id)
        self.assertIsNone(locations[0].identifier)
        self.assertEqual(locations[0].custom_fields, {})
        self.assertEqual(locations[1].identifier, "bambulab_1_0_0")
        self.assertEqual(
            locations[1].name,
            "Test Printer - AMS A1 [Printer 1]",
        )

    async def test_releasing_an_unknown_location_does_nothing(self):
        """Nothing to clean up before the driver ever assigned that tray."""
        spool_id = await self._spool_carrying({})

        await self.driver._release_slot_location(3, 3)

        async with self.sessions() as db:
            spool = await db.get(Spool, spool_id)
        self.assertIsNone(spool.location_id)

    def _reading_driver(self):
        """A driver that may recognise spools but not create or change them."""
        driver, _ = make_driver(auto_import_spools=False, sync_spool_weight=True)
        driver._loop = asyncio.get_running_loop()
        driver._auto_import_lock = asyncio.Lock()
        driver._printer_name = "Test Printer"
        driver._current_slots = [dict(self.slot)]
        return driver

    async def test_recognises_a_known_spool_without_importing(self):
        """Recognition is what assigns the slot, so it cannot depend on import."""
        driver = self._reading_driver()
        async with self.sessions() as db:
            status_id = await db.scalar(
                select(SpoolStatus.id).where(SpoolStatus.key == "opened")
            )
            known = Spool(
                filament_id=self.filament_id,
                status_id=status_id,
                rfid_uid="AABBCCDDEEFF0011AABBCCDDEEFF0011",
                remaining_weight_g=1000,
                custom_fields={},
            )
            db.add(known)
            await db.commit()
            await db.refresh(known)
            known_id = known.id

        await driver._auto_import_rfid_spools([self.slot])

        self.assertEqual(
            driver._spool_ids_by_tray_uuid,
            {"AABBCCDDEEFF0011AABBCCDDEEFF0011": known_id},
        )
        self.assertEqual(driver._slot_spool_ids, {"0-0": known_id})

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            untouched = await db.get(Spool, known_id)

        self.assertEqual(count, 1)
        self.assertIsNone(untouched.external_id)
        self.assertEqual(untouched.remaining_weight_g, 1000)
        self.assertEqual(untouched.custom_fields, {})

    async def test_unknown_tray_creates_nothing_without_the_import(self):
        driver = self._reading_driver()

        await driver._auto_import_rfid_spools([self.slot])

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))

        self.assertEqual(count, 0)
        self.assertEqual(driver._spool_ids_by_tray_uuid, {})

    async def test_valid_tray_uuid_imports_without_physical_tag_uid(self):
        slot = {**self.slot, "tag_uid": "0000000000000000"}

        await self.driver._auto_import_rfid_spools([slot])

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(count, 1)
        self.assertEqual(
            spool.external_id,
            "bambulab:AABBCCDDEEFF0011AABBCCDDEEFF0011",
        )
        self.assertNotIn(
            SPOOL_SYNC_MODULE.BAMBU_RFID_TAG_1_FIELD, spool.custom_fields
        )

    async def test_shop_image_refresh_does_not_require_physical_tag_uid(self):
        # tray_uuid alone is Bambu's identity (see README); tag_uid must not
        # additionally gate whether a shop-image lookup is scheduled.
        self.driver._resolve_shop_images = True
        self.driver._loop = Mock()
        slot = {k: v for k, v in self.slot.items() if k != "tag_uid"}

        # A newly booted CI runner can report a monotonic timestamp below the
        # throttle interval. An unseen slot must still schedule its first
        # refresh instead of treating timestamp zero as a previous attempt.
        with patch.object(CATALOG_MODULE.time, "monotonic", return_value=30):
            self.driver._schedule_shop_image_refresh([slot])
        self.driver._loop.call_soon_threadsafe.assert_called_once()

    async def test_physical_tag_uid_is_not_used_as_spool_identity(self):
        await self.driver._auto_import_rfid_spools([self.slot])
        replacement = {**self.slot, "tray_uuid": "11223344556677881122334455667788"}
        self.driver._current_slots = [replacement]
        await self.driver._auto_import_rfid_spools([replacement])

        async with self.sessions() as db:
            spools = list(
                (await db.execute(select(Spool).order_by(Spool.id))).scalars()
            )

        self.assertEqual(len(spools), 2)
        self.assertEqual(
            {spool.external_id for spool in spools},
            {
                "bambulab:AABBCCDDEEFF0011AABBCCDDEEFF0011",
                "bambulab:11223344556677881122334455667788",
            },
        )

    async def test_updates_existing_spool_from_bambu_estimate(self):
        await self.driver._auto_import_rfid_spools([self.slot])
        await self.driver._auto_import_rfid_spools(
            [{**self.slot, "remain": 42}]
        )

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(count, 1)
        self.assertEqual(spool.remaining_weight_g, 420)

    async def test_disabled_weight_sync_never_writes_bambu_estimate(self):
        self.driver._sync_spool_weight = False
        await self.driver._auto_import_rfid_spools([self.slot])

        async with self.sessions() as db:
            spool = (await db.execute(select(Spool))).scalar_one()
            self.assertIsNone(spool.remaining_weight_g)
            spool.remaining_weight_g = 600
            await db.commit()

        await self.driver._auto_import_rfid_spools(
            [{**self.slot, "remain": 42}]
        )

        async with self.sessions() as db:
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(spool.remaining_weight_g, 600)

    async def test_invalid_estimate_does_not_overwrite_existing_weight(self):
        await self.driver._auto_import_rfid_spools([self.slot])
        await self.driver._auto_import_rfid_spools(
            [{**self.slot, "tray_weight": "0", "remain": -1}]
        )

        async with self.sessions() as db:
            spool = (await db.execute(select(Spool))).scalar_one()

        self.assertEqual(spool.remaining_weight_g, 750)

    async def test_shop_image_url_is_cached_on_filament_not_physical_spool(self):
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "Matte - Charcoal (11101)"
            filament.manufacturer_color_name = "Charcoal (11101)"
            filament.shop_url = "https://eu.store.bambulab.com/products/pla-matte"
            await db.commit()

        expected_image = (
            "https://store.bblcdn.eu/s8/default/hash/charcoal.png"
            "__op__resize,m_lfit,w_640__op__format,f_auto__op__quality,q_80"
        )

        async def fake_fetch(_product_url):
            return {"11101": expected_image}

        self.driver._fetch_shop_product_images = fake_fetch
        metadata = await self.driver._cache_shop_image_for_filament(
            self.filament_id
        )

        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            spool_custom_fields = [
                spool.custom_fields
                for spool in (await db.execute(select(Spool))).scalars().all()
            ]

        self.assertEqual(metadata["shop_image_url"], expected_image)
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.BAMBU_SHOP_IMAGE_URL_FIELD],
            expected_image,
        )
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD],
            expected_image,
        )
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_PROVIDER_FIELD],
            "bambulab",
        )
        self.assertTrue(
            all(
                CATALOG_MODULE.BAMBU_SHOP_IMAGE_URL_FIELD not in (fields or {})
                for fields in spool_custom_fields
            )
        )

    async def test_spoolman_article_number_is_used_for_store_image_lookup(self):
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "PLA Matte - Charcoal"
            filament.manufacturer_color_name = "Charcoal"
            filament.custom_fields = {
                CATALOG_MODULE.ARTICLE_NUMBER_FIELD: "11101"
            }
            await db.commit()

        expected_image = "https://store.bblcdn.eu/product/charcoal-spool.jpg"
        expected_source = (
            "https://eu.store.bambulab.com/de/products/pla-matte"
            "?id=123456789"
        )

        async def search_image(product_code, product_url):
            self.assertEqual(product_code, "11101")
            self.assertIsNone(product_url)
            return {
                "shop_image_url": expected_image,
                "shop_source_url": expected_source,
            }

        self.driver._fetch_store_search_image = search_image
        metadata = await self.driver._cache_shop_image_for_filament(
            self.filament_id
        )

        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)

        self.assertEqual(metadata["bambu_product_code"], "11101")
        self.assertEqual(metadata["shop_image_url"], expected_image)
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.ARTICLE_NUMBER_FIELD],
            "11101",
        )
        self.assertNotIn(
            CATALOG_MODULE.BAMBU_PRODUCT_CODE_FIELD,
            filament.custom_fields,
        )
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD],
            expected_image,
        )

    async def test_legacy_product_code_backfills_article_number_on_cache_hit(self):
        cached_image = "https://store.bblcdn.eu/product/cached-charcoal.png"
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "PLA Matte - Charcoal"
            filament.manufacturer_color_name = "Charcoal"
            filament.custom_fields = {
                CATALOG_MODULE.BAMBU_PRODUCT_CODE_FIELD: "11101",
                CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD: cached_image,
                CATALOG_MODULE.FILAMENT_IMAGE_CHECKED_AT_FIELD: (
                    "2099-01-01T00:00:00+00:00"
                ),
                CATALOG_MODULE.BAMBU_IMAGE_RESOLVER_VERSION_FIELD: (
                    CATALOG_MODULE.STORE_SEARCH_RESOLVER_VERSION
                ),
            }
            await db.commit()

        async def unexpected_search(_product_code, _product_url=None):
            self.fail("a current image cache must not trigger store search")

        self.driver._fetch_store_search_image = unexpected_search
        metadata = await self.driver._cache_shop_image_for_filament(
            self.filament_id
        )

        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)

        self.assertEqual(metadata["bambu_product_code"], "11101")
        self.assertEqual(metadata["shop_image_url"], cached_image)
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.ARTICLE_NUMBER_FIELD],
            "11101",
        )

    async def test_refresh_shop_images_for_slots_continues_after_one_slot_fails(self):
        class _FakeFilament:
            def __init__(self, id):
                self.id = id

        slot_a = {**self.slot, "slot_index": "0-0"}
        slot_b = {**self.slot, "slot_index": "0-1"}

        async def fake_find(db, slot):
            return _FakeFilament(id=100 if slot["slot_index"] == "0-0" else 200)

        self.driver._find_matching_filament = fake_find

        calls = []

        async def fake_cache(filament_id):
            calls.append(filament_id)
            if filament_id == 100:
                raise RuntimeError("boom")
            return {"shop_image_url": "https://store.bblcdn.eu/ok.png"}

        self.driver._cache_shop_image_for_filament = fake_cache

        with self.assertLogs(level="WARNING"):
            await self.driver._refresh_shop_images_for_slots([slot_a, slot_b])

        self.assertEqual(sorted(calls), [100, 200])
        self.assertNotIn("0-0", self.driver._slot_display_metadata)
        self.assertEqual(
            self.driver._slot_display_metadata["0-1"]["shop_image_url"],
            "https://store.bblcdn.eu/ok.png",
        )

    async def test_offline_fallback_image_is_used_when_no_product_url_resolves(self):
        # No shop_url, no bambu_material_id/family match -> product_url stays
        # None; store search (mocked to no_store_search) also finds nothing.
        # The static offline-fallback image for this product code must still
        # reach shop_image_url instead of being discarded by the product_url
        # check.
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "Matte - Pure White (17100)"
            filament.manufacturer_color_name = "Pure White (17100)"
            await db.commit()

        async def unexpected_fetch(_product_url):
            self.fail("must not need to scrape a product page for the offline fallback")

        self.driver._fetch_shop_product_images = unexpected_fetch

        metadata = await self.driver._cache_shop_image_for_filament(self.filament_id)

        expected_image = Driver._optimized_shop_image_url(
            CATALOG_MODULE._BAMBU_PRODUCT_IMAGES_BY_CODE["17100"]
        )
        self.assertEqual(metadata["shop_image_url"], expected_image)

        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.BAMBU_SHOP_IMAGE_URL_FIELD],
            expected_image,
        )

    async def test_pla_pure_without_shop_url_uses_material_family_source(self):
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "Pure - Absolute Black (17101)"
            filament.manufacturer_color_name = "Absolute Black (17101)"
            filament.material_type = "PLA"
            filament.material_subgroup = "pure"
            filament.custom_fields = {
                "bambu_material_id": "GFA19",
                "bambu_detailed_filament_type": "PLA Pure",
            }
            await db.commit()

        expected_source = "https://eu.store.bambulab.com/products/pla-pure"
        expected_image = "https://store.bblcdn.eu/pure-white.jpg"

        async def fake_fetch(product_url):
            self.assertEqual(product_url, expected_source)
            return {"17101": expected_image}

        self.driver._fetch_shop_product_images = fake_fetch
        metadata = await self.driver._cache_shop_image_for_filament(
            self.filament_id
        )

        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)

        self.assertEqual(metadata["shop_image_url"], expected_image)
        self.assertEqual(filament.shop_url, expected_source)
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD],
            expected_image,
        )

    async def test_filascan_pla_pure_migrates_palette_to_store_search_media(self):
        palette_image = (
            "https://store.bblcdn.eu/s8/default/"
            "5939cb6c15514a7bb404738123d817c2/Pure_White.jpg"
        )
        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "PLA Pure · Pure White [GFA19-A19-W00]"
            filament.manufacturer_color_name = "Pure White"
            filament.material_type = "PLA"
            filament.material_subgroup = "PLA Pure"
            filament.custom_fields = {
                "bambu_material_id": "GFA19",
                "bambu_variant_id": "A19-W00",
                "bambu_color_code": "17100",
                "bambu_detailed_filament_type": "PLA Pure",
                CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD: palette_image,
                CATALOG_MODULE.BAMBU_SHOP_IMAGE_URL_FIELD: palette_image,
                CATALOG_MODULE.FILAMENT_IMAGE_CHECKED_AT_FIELD: (
                    "2026-08-09T10:00:00+00:00"
                ),
            }
            await db.commit()

        expected_image = "https://store.bblcdn.eu/product/pure-white-spool.jpg"
        expected_source = (
            "https://eu.store.bambulab.com/de/products/pla-pure"
            "?id=738891519803023418"
        )

        async def search_image(product_code, product_url):
            self.assertEqual(product_code, "17100")
            self.assertEqual(
                product_url,
                "https://eu.store.bambulab.com/products/pla-pure",
            )
            return {
                "shop_image_url": expected_image,
                "shop_source_url": expected_source,
            }

        async def unexpected_fetch(_product_url):
            self.fail("successful store search must not scrape the product page")

        self.driver._fetch_store_search_image = search_image
        self.driver._fetch_shop_product_images = unexpected_fetch
        metadata = await self.driver._cache_shop_image_for_filament(
            self.filament_id
        )

        async with self.sessions() as db:
            filament = await db.get(Filament, self.filament_id)

        self.assertEqual(filament.designation, "Pure - Pure White (17100)")
        self.assertEqual(filament.manufacturer_color_name, "Pure White (17100)")
        self.assertEqual(filament.material_subgroup, "pure")
        self.assertEqual(metadata["bambu_product_code"], "17100")
        self.assertEqual(metadata["shop_image_url"], expected_image)
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.ARTICLE_NUMBER_FIELD],
            "17100",
        )
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_URL_FIELD],
            expected_image,
        )
        self.assertEqual(
            filament.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_SOURCE_URL_FIELD],
            expected_source,
        )
        self.assertEqual(
            filament.custom_fields[
                CATALOG_MODULE.BAMBU_IMAGE_RESOLVER_VERSION_FIELD
            ],
            CATALOG_MODULE.STORE_SEARCH_RESOLVER_VERSION,
        )

    async def test_inventory_scan_resolves_only_bambu_filaments_with_spools(self):
        async with self.sessions() as db:
            manufacturer = (
                await db.execute(
                    select(Manufacturer).where(Manufacturer.name == "Bambu Lab")
                )
            ).scalar_one()
            filament = await db.get(Filament, self.filament_id)
            filament.designation = "Matte - Charcoal (11101)"
            filament.shop_url = "https://eu.store.bambulab.com/products/pla-matte"
            unopened = Filament(
                manufacturer_id=manufacturer.id,
                designation="PLA Basic - Red (10200)",
                material_type="PLA",
                diameter_mm=1.75,
                shop_url="https://eu.store.bambulab.com/products/pla-basic-filament",
            )
            db.add(unopened)
            await db.flush()
            opened_status = (
                await db.execute(
                    select(SpoolStatus).where(SpoolStatus.key == "opened")
                )
            ).scalar_one()
            db.add(Spool(filament_id=filament.id, status_id=opened_status.id))
            await db.commit()
            unopened_id = unopened.id

        async def fake_fetch(_product_url):
            return {
                "11101": "https://store.bblcdn.eu/charcoal.png",
                "10200": "https://store.bblcdn.eu/red.png",
            }

        self.driver._fetch_shop_product_images = fake_fetch
        stats = await self.driver._refresh_inventory_shop_images()

        async with self.sessions() as db:
            cached = await db.get(Filament, self.filament_id)
            unopened = await db.get(Filament, unopened_id)

        self.assertEqual(stats, {"filaments": 1, "images": 1})
        self.assertEqual(
            cached.custom_fields[CATALOG_MODULE.FILAMENT_IMAGE_PROVIDER_FIELD],
            "bambulab",
        )
        self.assertIsNone(unopened.custom_fields)

    async def test_shop_image_lock_is_shared_and_mutually_exclusive_across_drivers(self):
        second, _ = make_driver(printer_id=2)

        lock_a = self.driver._shop_image_lock_for(self.filament_id)
        lock_b = second._shop_image_lock_for(self.filament_id)
        # The lock is keyed by filament_id on the shared CatalogMixin class,
        # not per Driver instance -- two printers touching the same catalog
        # row must serialize through the exact same lock object.
        self.assertIs(lock_a, lock_b)

        order = []

        async def hold(lock, name):
            async with lock:
                order.append(f"{name}-enter")
                await asyncio.sleep(0.01)
                order.append(f"{name}-exit")

        await asyncio.gather(hold(lock_a, "a"), hold(lock_b, "b"))

        self.assertEqual(order, ["a-enter", "a-exit", "b-enter", "b-exit"])

    async def _add_bambu_filaments_with_spools(self, count: int) -> list[int]:
        async with self.sessions() as db:
            manufacturer = (
                await db.execute(
                    select(Manufacturer).where(Manufacturer.name == "Bambu Lab")
                )
            ).scalar_one()
            opened_status = (
                await db.execute(
                    select(SpoolStatus).where(SpoolStatus.key == "opened")
                )
            ).scalar_one()
            ids = []
            for i in range(count):
                filament = Filament(
                    manufacturer_id=manufacturer.id,
                    designation=f"PLA Basic - Color {i} (2{i:04d})",
                    material_type="PLA",
                    diameter_mm=1.75,
                )
                db.add(filament)
                await db.flush()
                db.add(Spool(filament_id=filament.id, status_id=opened_status.id))
                ids.append(filament.id)
            await db.commit()
        return ids

    async def test_inventory_scan_bounds_concurrency(self):
        await self._add_bambu_filaments_with_spools(
            ENRICHMENT_MODULE.INVENTORY_SCAN_CONCURRENCY + 2
        )

        concurrent = 0
        max_concurrent = 0

        async def fake_cache(filament_id):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.01)
            concurrent -= 1
            return {"shop_image_url": "https://store.bblcdn.eu/x.png"}

        self.driver._cache_shop_image_for_filament = fake_cache
        stats = await self.driver._refresh_inventory_shop_images()

        self.assertEqual(stats["filaments"], ENRICHMENT_MODULE.INVENTORY_SCAN_CONCURRENCY + 2)
        self.assertGreater(max_concurrent, 1, "scan must not be fully serial")
        self.assertLessEqual(max_concurrent, ENRICHMENT_MODULE.INVENTORY_SCAN_CONCURRENCY)

    async def test_inventory_scan_stops_after_driver_deregisters_mid_scan(self):
        await self._add_bambu_filaments_with_spools(3)

        original_concurrency = ENRICHMENT_MODULE.INVENTORY_SCAN_CONCURRENCY
        ENRICHMENT_MODULE.INVENTORY_SCAN_CONCURRENCY = 1
        calls = []

        async def fake_cache(filament_id):
            calls.append(filament_id)
            # Simulate stop() running concurrently with this scan.
            self.driver._running = False
            return {}

        self.driver._cache_shop_image_for_filament = fake_cache
        try:
            stats = await self.driver._refresh_inventory_shop_images()
        finally:
            ENRICHMENT_MODULE.INVENTORY_SCAN_CONCURRENCY = original_concurrency

        self.assertEqual(stats["filaments"], 3)
        self.assertEqual(len(calls), 1, "no filament after the stop() must start work")

    async def test_skips_spool_when_no_matching_filament_exists(self):
        unknown = {
            **self.slot,
            "tray_info_idx": "UNKNOWN",
            "tray_type": "ABS",
            "tray_color": "00FF00FF",
            "tag_uid": "8899AABBCCDDEEFF",
            "tray_uuid": "11223344556677881122334455667788",
        }
        self.driver._current_slots = [unknown]
        await self.driver._auto_import_rfid_spools([unknown])

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(Spool))
        self.assertEqual(count, 0)

    async def test_prefers_coded_filamentdb_record_for_charcoal(self):
        async with self.sessions() as db:
            manufacturer = (
                await db.execute(
                    select(Manufacturer).where(Manufacturer.name == "Bambu Lab")
                )
            ).scalar_one()
            black = Color(name="Black", hex_code="#000000")
            db.add(black)
            await db.flush()
            legacy = Filament(
                manufacturer_id=manufacturer.id,
                designation="Matte - Charcoal (11101)",
                manufacturer_color_name="Charcoal (11101)",
                material_type="PLA",
                material_subgroup="matte",
                diameter_mm=1.75,
                custom_fields={"filamentdb_id": 149},
            )
            duplicate = Filament(
                manufacturer_id=manufacturer.id,
                designation="PLA Matte Charcoal",
                manufacturer_color_name="Matte Charcoal",
                material_type="PLA",
                material_subgroup="matte",
                diameter_mm=1.75,
                custom_fields={"filamentdb_id": 24540},
            )
            db.add_all([legacy, duplicate])
            await db.flush()
            db.add_all(
                [
                    FilamentColor(
                        filament_id=legacy.id, color_id=black.id, position=1
                    ),
                    FilamentColor(
                        filament_id=duplicate.id, color_id=black.id, position=1
                    ),
                ]
            )
            await db.commit()

            match = await self.driver._find_matching_filament(
                db,
                {
                    "tray_info_idx": "GFA01",
                    "tray_type": "PLA",
                    "tray_sub_brands": "PLA Matte",
                    "tray_color": "000000FF",
                },
            )

        self.assertIsNotNone(match)
        self.assertEqual(match.custom_fields["filamentdb_id"], 149)

    async def test_collapses_same_silk_plus_product_code(self):
        async with self.sessions() as db:
            manufacturer = (
                await db.execute(
                    select(Manufacturer).where(Manufacturer.name == "Bambu Lab")
                )
            ).scalar_one()
            silver = Color(name="Silver", hex_code="#C8C8C8")
            db.add(silver)
            await db.flush()
            filaments = []
            for fdb_id, designation in (
                (22173, "Silk Plus - Silver"),
                (2850, "Silk Plus - Silver (13109)"),
                (22174, "Silk Plus - Silver (13109)"),
            ):
                filament = Filament(
                    manufacturer_id=manufacturer.id,
                    designation=designation,
                    manufacturer_color_name=designation.removeprefix("Silk Plus - "),
                    material_type="PLA",
                    material_subgroup="silk-plus",
                    diameter_mm=1.75,
                    custom_fields={"filamentdb_id": fdb_id},
                )
                db.add(filament)
                filaments.append(filament)
            await db.flush()
            db.add_all(
                [
                    FilamentColor(
                        filament_id=filament.id,
                        color_id=silver.id,
                        position=1,
                    )
                    for filament in filaments
                ]
            )
            await db.commit()

            match = await self.driver._find_matching_filament(
                db,
                {
                    "tray_info_idx": "GFA06",
                    "tray_type": "PLA",
                    "tray_sub_brands": "PLA Silk+",
                    "tray_color": "C8C8C8FF",
                },
            )

        self.assertIsNotNone(match)
        self.assertEqual(match.custom_fields["filamentdb_id"], 2850)

    async def test_different_product_codes_remain_ambiguous(self):
        async with self.sessions() as db:
            manufacturer = (
                await db.execute(
                    select(Manufacturer).where(Manufacturer.name == "Bambu Lab")
                )
            ).scalar_one()
            white = Color(name="White", hex_code="#FFFFFF")
            db.add(white)
            await db.flush()
            filaments = []
            for fdb_id, designation in (
                (76, "Jade White (10100)"),
                (999, "Other White (10105)"),
            ):
                filament = Filament(
                    manufacturer_id=manufacturer.id,
                    designation=designation,
                    manufacturer_color_name=designation,
                    material_type="PLA",
                    material_subgroup="basic",
                    diameter_mm=1.75,
                    custom_fields={"filamentdb_id": fdb_id},
                )
                db.add(filament)
                filaments.append(filament)
            await db.flush()
            db.add_all(
                [
                    FilamentColor(
                        filament_id=filament.id,
                        color_id=white.id,
                        position=1,
                    )
                    for filament in filaments
                ]
            )
            await db.commit()

            match = await self.driver._find_matching_filament(
                db,
                {
                    "tray_info_idx": "GFA00",
                    "tray_type": "PLA",
                    "tray_sub_brands": "PLA Basic",
                    "tray_color": "FFFFFFFF",
                },
            )

        self.assertIsNone(match)


class PrinterNameTests(unittest.IsolatedAsyncioTestCase):
    """Slot location names are built from the printer's own name.

    Regression guard for the name collision that made every location fall back
    to "Printer <id>": bambulabs_api also exports a class called ``Printer``,
    and importing it inside the same function shadowed the database model for
    that whole function.
    """

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.original_session_maker = DRIVER_MODULE.async_session_maker
        DRIVER_MODULE.async_session_maker = self.sessions
        async with self.sessions() as db:
            db.add(
                Printer(
                    id=1,
                    name="X1C",
                    driver_key="bambulab",
                    driver_config={},
                )
            )
            await db.commit()

    async def asyncTearDown(self):
        DRIVER_MODULE.async_session_maker = self.original_session_maker
        await self.engine.dispose()

    async def test_reads_the_name_from_the_database(self):
        driver, _ = make_driver()
        self.assertEqual(await driver._load_printer_name(), "X1C")

    async def test_falls_back_to_the_id_when_the_printer_is_unknown(self):
        driver, _ = make_driver(printer_id=99)
        self.assertEqual(await driver._load_printer_name(), "Printer 99")

    async def test_location_name_carries_the_printer_name(self):
        driver, _ = make_driver()
        driver._printer_name = await driver._load_printer_name()
        self.assertEqual(
            driver._generate_slot_location_name(0, 0), "X1C - AMS A1"
        )
        self.assertEqual(
            driver._generate_slot_location_name(255, 254), "X1C - ext. Slot 1"
        )

    async def test_loads_printer_name_before_mqtt_can_deliver_slots(self):
        """The first MQTT snapshot must already use the final display name."""
        driver, _ = make_driver()
        order = []

        class FakeMqttClient:
            def __init__(self):
                self._client = self
                self.on_connect_handler = None
                self.on_message_handler = None
                self.on_disconnect_handler = None

            def reconnect_delay_set(self, **_kwargs):
                return None

        class FakeBambuPrinter:
            def __init__(self):
                self.mqtt_client = FakeMqttClient()

            def mqtt_start(self):
                order.append(("mqtt", driver._printer_name))

        async def load_name():
            order.append(("name", None))
            return "X1C"

        driver._load_printer_name = load_name
        driver._ensure_shared_extra_fields = AsyncMock()
        fake_printer = FakeBambuPrinter()

        with patch("bambulabs_api.Printer", return_value=fake_printer):
            await driver.start()

        self.assertEqual(order, [("name", None), ("mqtt", "X1C")])


if __name__ == "__main__":
    unittest.main()
