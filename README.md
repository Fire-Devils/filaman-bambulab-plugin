# Bambu Lab Plugin for FilaMan

A FilaMan printer driver plugin that connects to Bambu Lab printers via MQTT, reads AMS slot data in real-time and enables automatic filament-to-tray assignment.

## Features

- MQTT communication via `bambulabs_api`
- AMS and AMS Lite support (auto-detection by printer model)
- External tray support
- RFID spool identification
- Optional automatic import of Bambu RFID spools
- Optional estimated-weight synchronization, disabled by default
- Dedicated responsive AMS and spool overview page
- Optional color-specific Bambu product images loaded directly from Bambu's CDN, with persistent URL metadata
- Read-only mode for status and inventory synchronization without printer changes
- Automatic spool-to-tray matching with a 60-second default timeout (caller-overridable)
- Automatic FilaMan location updates after successful AMS assignments
- 10 printer-specific parameters (material index, calibration, temperatures, flow)
- Protocol-level debug logging (viewable in admin UI)
- Auto-reconnect with configurable interval
- Automatic migration of legacy Bambu printer-parameter keys

## Supported Printers

P1S, P1P, X1 Carbon, X1E, A1, A1 Mini, H2C, H2D, H2S, P2S

## Installation

The recommended installation method is to upload the release ZIP through FilaMan's plugin management page. Restart FilaMan if the interface asks you to do so.

For a manual installation, copy the complete `bambulab/` folder into the FilaMan plugins directory and restart FilaMan. Do not copy only the individual files from inside that folder.

## Code Structure

The Python implementation is split by responsibility while keeping `driver.py` as FilaMan's required entry point:

- `driver.py` coordinates lifecycle, MQTT callbacks, printer commands and the public driver API.
- `slots.py` normalizes identifiers and parses AMS, AMS Lite, AMS HT and external tray payloads.
- `spool_sync.py` owns safe filament matching, RFID spool upserts, remaining weight and managed locations.
- `catalog.py` resolves and stores Bambu product image metadata without downloading image binaries into FilaMan.
- `catalog_enrichment.py` coordinates event-driven and periodic inventory image scans across driver instances.
- `state.py` contains the small runtime state objects shared by the driver modules.

Every module, class and function contains a short docstring explaining its role. The separation is intentionally internal: FilaMan still loads `bambulab.driver.Driver`, and the plugin manifest and runtime API remain unchanged.

## Configuration

Create a new printer in the FilaMan admin panel and select **Bambu Lab** as driver. The following fields are required:

| Field | Description |
|-------|-------------|
| Printer Model | Your Bambu Lab model (determines AMS data handling) |
| IP/Hostname | IP address or hostname of the printer |
| Serial Number | Printer serial number |
| Access Code | Printer access code |
| Read-only Mode | Prevents all state-changing MQTT commands (default: off) |
| Automatically Import Bambu RFID Spools | Adds unknown RFID spools (default: off) |
| Synchronize Estimated Spool Weight | Sets and updates remaining weight from Bambu's estimate during automatic import (default: off) |
| Catalog Images (Bambu Lab) | Resolves color-specific images through Bambu's EU Store Search API, with a product-page fallback, and stores only their URLs (default: off) |
| Reconnect Interval | Minutes between reconnection attempts (default: 5) |

> **Important:** Sending filament settings (spool assignment) requires the printer to be in **LAN-only mode** with **Developer Mode** enabled. Without these settings, only reading AMS status is possible. This is a Bambu Lab firmware restriction.

## Bambu RFID Spool Import

Bambu RFID trays report two different identifiers:

- `tray_uuid` identifies the logical Bambu spool and is its sole Bambu identity. It is normalized to 32 uppercase hexadecimal characters and stored as `external_id=bambulab:<tray_uuid>` for duplicate prevention.
- `tag_uid` identifies only one physical RFID chip. When present, the first UID seen via MQTT is retained as optional metadata in **Bambu RFID Tag 1**. It is neither required for importing nor used to match a spool.

FilaMan's built-in `rfid_uid` field is never written or overwritten by this plugin and remains available for custom RFID tags. For compatibility with Spoolman imports and external readers that stored the Bambu `tray_uuid` there, automatic import checks a normalized 32-character `rfid_uid` before creating a spool. A match reuses the existing spool and, when its `external_id` is empty, adds the canonical `bambulab:<tray_uuid>` external ID without changing `rfid_uid`.

At driver startup, the plugin ensures that the shared filament field **Bambu Color Code (Article Number)** (`article_number`) and the system spool extra fields **Bambu RFID Tag 1** (`bambu_rfid_tag_1`) and **Bambu RFID Tag 2** (`bambu_rfid_tag_2`) exist. If another integration such as FilaScan already registered `article_number`, its definition and ownership are retained. The first RFID field can receive the MQTT `tag_uid`; the second remains available for another reader or integration.

Automatic import never creates manufacturers, colors, or filaments. A new spool is only created when the plugin finds one safe existing filament match, first by its `bambu_tray_idx` printer parameter and then by Bambu manufacturer, material, subtype, and color. Duplicate FilamentDB records for the same Bambu product are collapsed deterministically, preferring the canonical record carrying a five-digit Bambu product code such as `(11101)`. Matches across genuinely different product codes remain blocked and are logged. Repeated MQTT messages for the same `tray_uuid` do not create duplicate spools.

For valid Bambu RFID spools, MQTT reports the nominal filament weight in `tray_weight` and the estimated percentage in `remain`. When **Synchronize Estimated Spool Weight** is enabled together with automatic import, the plugin calculates `tray_weight × remain / 100` and writes `remaining_weight_g` for both newly created and already existing spools. The option is disabled by default, so no estimated weight is written unless the administrator explicitly enables it. Missing or invalid estimates (`tray_weight = 0`, `remain = -1`) never overwrite an existing weight.

An all-zero `tray_uuid` placeholder from a custom or non-RFID spool is ignored. A missing or all-zero `tag_uid` does not prevent importing a spool with a valid `tray_uuid`. The slot parser supports both the legacy `vt_tray` object and the newer `vir_slot` list used for multiple external trays, as well as AMS HT unit IDs.

Read-only mode and automatic import are independent: enabling both lets the plugin read the AMS and update FilaMan while preventing changes to the printer.

FilaMan locations for printer slots use a stable internal identifier based on the printer and slot IDs. The visible location name contains the printer name and is updated safely when that printer is renamed; equally named printers receive distinct locations. This prevents MQTT startup timing and display-name changes from creating or reusing the wrong AMS location. A direct present-to-present spool swap is detected from its `tray_uuid`, so the previous slot assignment and image metadata cannot leak into the replacement spool. Identifier-less legacy locations are adopted only when plugin ownership and the printer/slot match, or when one location unambiguously uses the historical `Printer <id> - ...` name. Manual and foreign locations are not claimed by name alone.

## Spool Gallery, AMS Overview and Catalog Images

The plugin registers **Bambu Lab** as a navigation page at `/plugin-page/bambulab` with two views:

- **All Spools** displays every FilaMan spool, regardless of manufacturer, with search, manufacturer/material filters, archived-spool visibility, remaining weight, status and RFID state. Spools without a catalog image use a colorized local illustration.
- **AMS Live** keeps the existing printer-centric view of active Bambu printers, AMS slots, linked FilaMan spools, RFID state, estimated remaining percentage and synchronized remaining weight.

FilaMan serves third-party `page.html` files directly rather than through its
built-in Astro layout. The page therefore remains standalone, uses FilaMan's
saved brand/light/dark theme and provides a translated back button that returns
to the previous FilaMan page (or the dashboard when no internal referrer is
available). It reads the signed-in user's language from `/api/v1/me` (with
FilaMan's `localStorage.lang` as the early-load fallback) and includes complete
English and German UI dictionaries following FilaMan's `data-i18n` convention.

When **Catalog Images (Bambu Lab)** is enabled, genuine Bambu RFID trays can trigger a low-frequency metadata lookup. The plugin also listens for FilaMan's standard `spools_changed` and `filaments_changed` events, so Bambu inventory created by independent integrations such as FilaScan or Bambu Cloud Connect is enriched immediately. Bursts of events are debounced into one scan, and multiple Bambu printer drivers share one process-wide enrichment worker. A startup scan and a scan every six hours remain as a fallback for events emitted while the plugin was offline. Opening **All Spools** or pressing **Refresh** now also requests an explicit scan through FilaMan's primary driver worker. This provides immediate reconciliation when an import event was emitted in another web-worker process. The scan is best-effort: an offline printer, unavailable primary driver or image-service failure never blocks the all-spool inventory, and AMS refresh falls back to the cached offline health state. Only Bambu/Bambu Lab filaments with at least one physical FilaMan spool are scanned, deliberately excluding unused FilamentDB catalog entries. The resolver searches Bambu's EU Store API by the filament's five-digit product code (for example `11101`) and accepts exactly one highlighted product result. It uses the result's `mediaFiles[0]` product image rather than the `colorPalette` swatch, and links directly to the selected `highlightProductSkuId`. Product-page color data remains a fallback if the search API is unavailable. A custom/non-RFID AMS tray never triggers an AMS-based lookup.

Image URL metadata is persisted in the matching shared **filament's** `custom_fields`: `filament_image_url`, `filament_image_source_url`, `filament_image_provider` and `filament_image_checked_at`. Compatibility copies remain in the existing `bambu_shop_*` fields. This generic field scheme lets later manufacturer resolvers feed the same gallery without changing its UI. Every physical spool made from that filament automatically reuses the metadata. For known new Bambu families such as `GFA19` / PLA Pure, the plugin can supply the official product-family URL when an independently created filament does not yet have a `shop_url`. Current storefront color swatches are preferred over generic JSON-LD product images.

The shared filament's `article_number` is the canonical manufacturer product
code and is checked first. FilaScan's raw `bambu_color_code` and the legacy
`bambu_product_code` remain accepted as compatibility sources. Only a standalone
five-digit code is used. When a code is derived from a legacy field or the
filament name, the plugin fills an empty `article_number` but never overwrites an
existing value. If an independently imported filament still has FilaScan's
exact technical fallback name, the Bambu plugin
safely normalizes it to the FilamentDB-style display name during enrichment.
For PLA Pure White `17100`, the previous palette image is automatically
re-resolved through the Store API. A verified Bambu CDN swatch URL remains only
as an offline fallback.

Image binaries are not downloaded into FilaMan. Each browser loads an enabled image directly from the manufacturer's CDN when it is displayed. A browser may reuse its own HTTP cache, but the plugin does not provide or guarantee a local binary-image cache. Saved URL metadata is refreshed after 30 days. Failed lookup attempts are suppressed for up to one day in the running plugin process. If an image is unavailable, both views fall back to the filament or MQTT tray color.

### Stored Fields and Compatibility

The plugin intentionally keeps several current and legacy fields so existing
FilaMan, FilamentDB, FilaScan and Spoolman installations continue to work.
Fields are not removed automatically merely because a newer canonical field is
available.

| Field | Target | Behavior and purpose |
|---|---|---|
| `article_number` | Filament | Visible canonical manufacturer product/color code shared with FilaScan. It is checked first for Bambu Store image lookup. A standalone five-digit code derived from a legacy field or filament name fills this field only when it is empty; an existing value is never overwritten. |
| `filament_image_url` | Filament | Canonical product image URL used by the all-spool gallery. |
| `filament_image_source_url` | Filament | Canonical product-page URL associated with the image. |
| `filament_image_provider` | Filament | Identifies the metadata provider, currently `bambulab`. |
| `filament_image_checked_at` | Filament | Timestamp used for the 30-day metadata refresh interval. |
| `bambu_shop_image_url`, `bambu_shop_source_url`, `bambu_shop_image_checked_at` | Filament | Legacy compatibility copies of the generic image fields. They remain readable and writable for existing installations and may be migrated in a future release. |
| `bambu_image_resolver_version` | Filament | Records the Bambu Store resolver generation. It is retained for compatibility; a future migration may replace it with a non-`bambu_` key. |
| `bambu_product_code` | Filament | Legacy product-code source. It is still read but is no longer written for new resolutions; `article_number` is canonical. |
| `bambu_color_code` | Filament | FilaScan-compatible raw product-code source. It remains read-only from this plugin's perspective. |
| `bambu_material_id`, `bambu_variant_id`, `bambu_detailed_filament_type` | Filament | Optional metadata supplied by independent integrations such as FilaScan. It is used to identify product families and normalize exact generated fallback profiles, but is not deleted or overwritten by this plugin. |
| `bambu_rfid_tag_1`, `bambu_rfid_tag_2` | Spool | Optional metadata for the two physical Bambu RFID chips. The built-in `rfid_uid` remains untouched. |

The plugin manifest also defines the printer-specific parameters
`bambu_idx`, `bambu_tray_idx`, `bambu_setting_id`, `bambu_cali_idx`,
`bambu_k_value`, `bambu_flow_ratio`, `bambu_bed_temp`,
`bambu_nozzle_temp_min`, `bambu_nozzle_temp_max` and
`bambu_max_volumetric_speed` for both filament and spool overrides.
`bambu_idx`, `bambu_tray_idx` and the nozzle-temperature parameters participate
directly in current matching or tray assignment. The remaining calibration and
profile parameters are retained for compatibility with existing FilaMan,
Spoolman and original-plugin workflows. They should not be removed without a
dedicated data migration and a compatibility review.

## A Note to Bambu Lab

Dear Bambu Lab,

This independent, non-commercial community plugin optionally retrieves official product-image URLs from the Bambu Lab EU Store service and displays the corresponding images directly from Bambu Lab's CDN. Its purpose is to help users visually identify physical Bambu Lab filament spools in their own FilaMan inventory.

When an exact product match is available, the plugin also links directly to the official Bambu Lab product page and selected SKU, allowing the user to view the product or order another spool. The plugin adds no affiliate, referral, advertising, or tracking parameters to these links, and its authors receive no commission or other commercial benefit from them.

The image feature is disabled by default. FilaMan stores only the image and product-page URLs; the plugin package does not contain or redistribute Bambu Lab image files. Each user's browser retrieves enabled images directly from Bambu Lab's CDN. This project is not affiliated with or endorsed by Bambu Lab, and this note does not claim ownership of or a license to Bambu Lab content. If Bambu Lab prefers a different integration method or requests removal of this optional feature, please contact the project maintainers.

## Printer Parameters

The plugin registers the following per-printer parameters for filaments and spools:

| Parameter | Type | Description |
|-----------|------|-------------|
| Bambu Material Index | Dropdown | Material code from `bambu_filaments.json` |
| Tray Info Index | Text | Tray info index string |
| Setting ID | Text | Bambu setting identifier |
| Calibration Index | Text | Calibration index |
| K Value | Number | Pressure advance K value |
| Flow Ratio | Number | Flow ratio multiplier |
| Bed Temperature | Number | Bed temperature (°C) |
| Nozzle Temp Min | Number | Minimum nozzle temperature (°C) |
| Nozzle Temp Max | Number | Maximum nozzle temperature (°C) |
| Max Volumetric Speed | Number | Maximum volumetric speed (mm³/s) |

Parameters can be set at filament level (shared across spools) or overridden per individual spool.

## Development and Releases

Commit subjects use the [Conventional Commits](https://www.conventionalcommits.org/)
format, for example `feat(catalog): add a product resolver` or
`fix: reuse an existing spool`. Pull requests should preferably be squash-merged
with a Conventional Commit title. A breaking change is declared with `!` in the
subject or a `BREAKING CHANGE:` footer.

The GitHub release workflow treats `bambulab/plugin.json` as the authoritative
version source and generates release notes from direct commits since the
previous `bambulab-v*` tag. It groups features, fixes and other change types,
ignores merge commits and `chore: bump plugin version ...`, and rejects other
nonconventional direct commit subjects. Before publishing, it validates the
exact SemVer transition: `fix` and maintenance changes require a patch release,
`feat` requires a minor release, and a breaking change requires a major release.
The pure-Python generator and its tests live in `scripts/generate_release_notes.py`
and `tests/test_release_notes.py`.

## AI-Assisted Development

This plugin has been developed with the assistance of generative AI tools, including OpenAI Codex. AI assistance has been used for parts of the implementation, refactoring, testing, debugging and documentation. The project is not presented as exclusively human-written; its human maintainers remain responsible for reviewing, accepting and publishing all changes.

## License

See the [FilaMan](https://github.com/Fire-Devils/FilaMan) project for license information.
