"""Resolve and persist Bambu product image metadata.

This module contains the HTTP client, HTML/JSON parsing, URL validation and
per-filament metadata cache. It deliberately stores URLs only; image binaries
remain on the manufacturer's CDN.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import async_session_maker
from app.core.event_bus import event_bus
from app.models.filament import Filament
from app.models.spool import Spool

from .slots import _BAMBU_PRODUCT_CODE_RE

logger = logging.getLogger(__name__)

SHOP_IMAGE_CACHE_DAYS = 30
SHOP_IMAGE_ERROR_RETRY_SECONDS = 24 * 60 * 60
SHOP_IMAGE_SCHEDULE_SECONDS = 60
SHOP_PAGE_MEMORY_CACHE_SECONDS = 6 * 60 * 60
STORE_SEARCH_API_URL = (
    "https://eu-store-api.bambulab.com/mall-goods/product/globalSearchV2"
)
STORE_SEARCH_REGION = "EU"
STORE_SEARCH_RESOLVER_VERSION = 2
STORE_SEARCH_MAX_BYTES = 5 * 1024 * 1024
SHOP_PAGE_MAX_BYTES = 5 * 1024 * 1024
BAMBU_SHOP_IMAGE_URL_FIELD = "bambu_shop_image_url"
BAMBU_SHOP_SOURCE_URL_FIELD = "bambu_shop_source_url"
BAMBU_SHOP_IMAGE_CHECKED_AT_FIELD = "bambu_shop_image_checked_at"
BAMBU_PRODUCT_CODE_FIELD = "bambu_product_code"  # Legacy read compatibility only
BAMBU_IMAGE_RESOLVER_VERSION_FIELD = "bambu_image_resolver_version"
ARTICLE_NUMBER_FIELD = "article_number"
_BAMBU_PRODUCT_URLS_BY_MATERIAL_ID = {
    "GFA19": "https://eu.store.bambulab.com/products/pla-pure",
}
_BAMBU_PRODUCT_URLS_BY_FAMILY = {
    "pla pure": "https://eu.store.bambulab.com/products/pla-pure",
}
_BAMBU_PRODUCT_IMAGES_BY_CODE = {
    "17100": (
        "https://store.bblcdn.eu/s8/default/"
        "5939cb6c15514a7bb404738123d817c2/Pure_White.jpg"
    ),
}
FILAMENT_IMAGE_URL_FIELD = "filament_image_url"
FILAMENT_IMAGE_SOURCE_URL_FIELD = "filament_image_source_url"
FILAMENT_IMAGE_PROVIDER_FIELD = "filament_image_provider"
FILAMENT_IMAGE_CHECKED_AT_FIELD = "filament_image_checked_at"


def _evict_expired(cache: dict[Any, float], ttl: float, now: float) -> None:
    """Drop entries whose timestamp value is older than ttl, in place."""
    for key in [k for k, ts in cache.items() if now - ts >= ttl]:
        del cache[key]


def _evict_expired_timestamped(
    cache: dict[Any, tuple[float, Any]], ttl: float, now: float
) -> None:
    """Drop entries whose (timestamp, value) tuple is older than ttl, in place."""
    for key in [k for k, v in cache.items() if now - v[0] >= ttl]:
        del cache[key]


class _JsonLdScriptParser(HTMLParser):
    """Collect JSON-LD script bodies without depending on an HTML package."""

    def __init__(self) -> None:
        """Initialize the parser's current script buffer and result list."""
        super().__init__(convert_charrefs=True)
        self._collecting = False
        self._buffer: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start collecting when an ``application/ld+json`` script begins."""
        if tag.lower() != "script":
            return
        attr_map = {str(key).lower(): value for key, value in attrs}
        if str(attr_map.get("type") or "").lower() == "application/ld+json":
            self._collecting = True
            self._buffer = []

    def handle_data(self, data: str) -> None:
        """Append text belonging to the current JSON-LD script."""
        if self._collecting:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Finalize and store the current JSON-LD script body."""
        if tag.lower() == "script" and self._collecting:
            self.scripts.append("".join(self._buffer))
            self._collecting = False
            self._buffer = []



class CatalogMixin:
    """Provide product lookup, validation and metadata caching to the driver."""

    # Shared across ALL Driver instances (class attributes, not per-instance):
    # the Filament row _cache_shop_image_for_filament reads/writes is shared
    # between printers, so the lock serializing access to it must be too —
    # a per-instance lock (as used before) fails to serialize concurrent
    # writes from two Bambu printers that share the same catalog filament.
    _shop_image_locks: dict[int, asyncio.Lock] = {}
    _shop_image_last_attempt: dict[int, float] = {}

    @classmethod
    def _shop_image_lock_for(cls, filament_id: int) -> asyncio.Lock:
        """Return the shared lock guarding one filament's catalog row."""
        lock = cls._shop_image_locks.get(filament_id)
        if lock is None:
            lock = asyncio.Lock()
            cls._shop_image_locks[filament_id] = lock
        return lock

    @staticmethod
    def _bambu_product_code(*values: Any) -> str | None:
        """Extract the first standalone five-digit Bambu color/product code."""
        for value in values:
            match = _BAMBU_PRODUCT_CODE_RE.search(str(value or ""))
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _allowed_shop_url(value: Any) -> str | None:
        """Accept only public Bambu store product pages used by FilamentDB."""
        try:
            parsed = urlparse(str(value or "").strip())
        except ValueError:
            return None
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            hostname == "store.bambulab.com"
            or hostname.endswith(".store.bambulab.com")
        ):
            return None
        # Variant IDs differ between regions and are unnecessary for resolving
        # the color code.  Dropping the query also improves page-cache reuse.
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    @staticmethod
    def _allowed_shop_image_url(value: Any) -> str | None:
        """Accept HTTPS images only from known Bambu or Shopify CDN hosts."""
        try:
            parsed = urlparse(str(value or "").strip())
        except ValueError:
            return None
        hostname = (parsed.hostname or "").lower()
        allowed = (
            hostname in {"store.bblcdn.com", "store.bblcdn.eu", "cdn.shopify.com"}
            or hostname.endswith(".bblcdn.com")
            or hostname.endswith(".bblcdn.eu")
        )
        if parsed.scheme != "https" or not allowed:
            return None
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))

    @classmethod
    def _optimized_shop_image_url(cls, value: Any) -> str | None:
        """Validate a CDN URL and add Bambu's bounded image transformation."""
        image_url = cls._allowed_shop_image_url(value)
        if not image_url:
            return None
        parsed = urlparse(image_url)
        hostname = (parsed.hostname or "").lower()
        if "__op__" in parsed.path or "bblcdn" not in hostname:
            return image_url
        optimized_path = (
            f"{parsed.path}__op__resize,m_lfit,w_640"
            "__op__format,f_auto__op__quality,q_80"
        )
        return urlunparse(
            (parsed.scheme, parsed.netloc, optimized_path, "", parsed.query, "")
        )

    @classmethod
    def _extract_shop_product_images(cls, html: str) -> dict[str, str]:
        """Return Bambu five-digit product code -> color image URL."""
        parser = _JsonLdScriptParser()
        parser.feed(html)
        images: dict[str, str] = {}

        # The current Bambu storefront exposes the actual color image in its
        # swatch markup and embedded product data. Prefer it over the generic
        # product image that some JSON-LD variants carry.
        swatch_pattern = re.compile(
            r'<li\b[^>]*\bvalue=["\'][^"\']*?(?P<code>\d{5})[^"\']*["\']'
            r'(?:(?!</li>).){0,2000}?<img\b[^>]*\bsrc=["\'](?P<url>https://[^"\']+)',
            re.IGNORECASE | re.DOTALL,
        )
        for match in swatch_pattern.finditer(html):
            image_url = cls._optimized_shop_image_url(match.group("url"))
            if image_url:
                images.setdefault(match.group("code"), image_url)

        embedded_product_data = html.replace(r'\"', '"')
        embedded_pattern = re.compile(
            r'"value"\s*:\s*"[^"]*?(?P<code>\d{5})[^"]*"'
            r'.{0,2000}?"colorUrl"\s*:\s*"(?P<url>https://[^"]+)',
            re.IGNORECASE | re.DOTALL,
        )
        for match in embedded_pattern.finditer(embedded_product_data):
            image_url = cls._optimized_shop_image_url(match.group("url"))
            if image_url:
                images.setdefault(match.group("code"), image_url)

        def visit(value: Any) -> None:
            """Recursively collect product-code images from JSON-LD values."""
            if isinstance(value, list):
                for child in value:
                    visit(child)
                return
            if not isinstance(value, dict):
                return

            name = value.get("name")
            product_code = cls._bambu_product_code(name)
            image_value = value.get("image")
            if isinstance(image_value, list):
                image_value = image_value[0] if image_value else None
            elif isinstance(image_value, dict):
                image_value = image_value.get("url") or image_value.get("contentUrl")
            image_url = cls._optimized_shop_image_url(image_value)
            if product_code and image_url:
                images.setdefault(product_code, image_url)

            for key in ("hasVariant", "@graph", "itemListElement"):
                if key in value:
                    visit(value[key])

        for script in parser.scripts:
            try:
                visit(json.loads(script))
            except (json.JSONDecodeError, TypeError):
                continue
        return images

    @classmethod
    def _parse_store_search_product(
        cls,
        payload: Any,
        expected_product_url: str | None = None,
    ) -> dict[str, str] | None:
        """Return the unique highlighted product image from Bambu store search."""
        if not isinstance(payload, dict) or payload.get("code") != 1:
            return None
        page = (payload.get("data") or {}).get("page")
        if not isinstance(page, dict) or str(page.get("total")) != "1":
            return None
        records = page.get("records")
        if not isinstance(records, list) or len(records) != 1:
            return None
        record = records[0]
        if not isinstance(record, dict):
            return None
        sku_id = str(record.get("highlightProductSkuId") or "").strip()
        if not re.fullmatch(r"\d+", sku_id):
            return None

        seo_code = str(record.get("seoCode") or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9-]+", seo_code):
            return None
        product_family_url = cls._allowed_shop_url(
            f"https://eu.store.bambulab.com/de/products/{seo_code}"
        )
        if not product_family_url:
            return None
        expected_url = cls._allowed_shop_url(expected_product_url)
        if (
            expected_url
            and urlparse(expected_url).path.rstrip("/").split("/")[-1]
            != seo_code
        ):
            return None

        media_files = record.get("mediaFiles")
        if not isinstance(media_files, list):
            return None
        image_url = next(
            (
                optimized
                for value in media_files
                if (optimized := cls._optimized_shop_image_url(value))
            ),
            None,
        )
        if not image_url:
            return None
        return {
            "shop_image_url": image_url,
            "shop_source_url": f"{product_family_url}?id={sku_id}",
        }

    async def _fetch_store_search_image(
        self,
        product_code: str,
        expected_product_url: str | None = None,
    ) -> dict[str, str] | None:
        """Resolve one exact product image through Bambu's EU Store Search API."""
        expected_url = self._allowed_shop_url(expected_product_url)
        expected_slug = (
            urlparse(expected_url).path.rstrip("/").split("/")[-1]
            if expected_url
            else ""
        )
        cache_key = f"{product_code}:{expected_slug}"
        now = time.monotonic()
        _evict_expired_timestamped(self._store_search_cache, SHOP_PAGE_MEMORY_CACHE_SECONDS, now)
        cached = self._store_search_cache.get(cache_key)
        if cached and now - cached[0] < SHOP_PAGE_MEMORY_CACHE_SECONDS:
            return dict(cached[1]) if cached[1] else None

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-BBL-STORE-REGION": STORE_SEARCH_REGION,
            "User-Agent": "FilaMan-BambuLab-Plugin/2.6 (store-search-image)",
        }
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            headers=headers,
        ) as client:
            response = await client.post(
                STORE_SEARCH_API_URL,
                json={"content": product_code, "current": 1, "size": 4},
            )
            response.raise_for_status()
            if len(response.content) > STORE_SEARCH_MAX_BYTES:
                raise ValueError("Bambu store search response exceeds the safety limit")
            result = self._parse_store_search_product(
                response.json(), expected_url
            )

        self._store_search_cache[cache_key] = (now, result)
        return dict(result) if result else None

    @classmethod
    def _catalog_product_url(
        cls, filament: Filament, custom_fields: dict[str, Any]
    ) -> str | None:
        """Infer an official product-family URL from trusted Bambu metadata."""
        material_id = str(custom_fields.get("bambu_material_id") or "").upper()
        if material_id in _BAMBU_PRODUCT_URLS_BY_MATERIAL_ID:
            return cls._allowed_shop_url(
                _BAMBU_PRODUCT_URLS_BY_MATERIAL_ID[material_id]
            )

        family_values = (
            custom_fields.get("bambu_detailed_filament_type"),
            f"{filament.material_type or ''} {filament.material_subgroup or ''}",
        )
        for value in family_values:
            normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())
            normalized = " ".join(normalized.split())
            if normalized in _BAMBU_PRODUCT_URLS_BY_FAMILY:
                return cls._allowed_shop_url(
                    _BAMBU_PRODUCT_URLS_BY_FAMILY[normalized]
                )
        return None

    @classmethod
    def _normalize_generated_bambu_profile(
        cls,
        filament: Filament,
        custom_fields: dict[str, Any],
        product_code: str | None,
    ) -> bool:
        """Repair only the exact technical profile emitted by FilaScan.

        FilaScan and this driver remain independent.  The shared Bambu
        metadata provides enough evidence to safely recognize the generated
        fallback without touching catalog or user-named filaments.
        """
        material_id = str(custom_fields.get("bambu_material_id") or "").upper()
        variant_id = str(custom_fields.get("bambu_variant_id") or "").upper()
        detailed_type = str(
            custom_fields.get("bambu_detailed_filament_type") or ""
        ).strip()
        color_name = str(filament.manufacturer_color_name or "").strip()
        if not all((material_id, variant_id, detailed_type, color_name, product_code)):
            return False

        legacy_designations = {
            f"{detailed_type} · {color_name} [{material_id}-{variant_id}]".casefold(),
            (
                f"Bambu {detailed_type} · {color_name} "
                f"[{material_id}-{variant_id}]"
            ).casefold(),
        }
        if filament.designation.casefold() not in legacy_designations:
            return False

        family = re.sub(
            r"^Bambu(?:\s+Lab)?\s+", "", detailed_type, flags=re.IGNORECASE
        )
        family = re.sub(
            rf"^{re.escape(str(filament.material_type or ''))}(?:\s+|-)?",
            "",
            family,
            flags=re.IGNORECASE,
        ).strip(" -")
        family_names = {
            "hf": "Hf",
            "silk+": "Silk Plus",
            "silk plus": "Silk Plus",
        }
        family = family_names.get(family.casefold(), family)
        color_label = (
            color_name
            if cls._bambu_product_code(color_name) == product_code
            else f"{color_name} ({product_code})"
        )
        filament.designation = (
            f"{family} - {color_label}" if family.casefold() != "basic" else color_label
        )
        filament.manufacturer_color_name = color_label
        filament.material_subgroup = (
            re.sub(r"[^a-z0-9]+", "-", family.casefold()).strip("-") or None
        )
        return True

    @staticmethod
    def _parse_cache_timestamp(value: Any) -> datetime | None:
        """Parse an ISO timestamp and normalize it to timezone-aware UTC."""
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _slot_identity(cls, slot: dict[str, Any]) -> str:
        """Build a stable cache identity for the current slot contents."""
        tray_uuid = cls._normalize_hex_identifier(slot.get("tray_uuid"), 32)
        if tray_uuid:
            return f"uuid:{tray_uuid}"
        return "|".join(
            str(slot.get(key) or "")
            for key in (
                "slot_index",
                "tray_info_idx",
                "tray_type",
                "tray_color",
                "tray_sub_brands",
            )
        )

    async def _fetch_shop_product_images(self, product_url: str) -> dict[str, str]:
        """Download one allowed product page and extract its color images."""
        now = time.monotonic()
        _evict_expired_timestamped(self._shop_page_cache, SHOP_PAGE_MEMORY_CACHE_SECONDS, now)
        cached = self._shop_page_cache.get(product_url)
        if cached and now - cached[0] < SHOP_PAGE_MEMORY_CACHE_SECONDS:
            return dict(cached[1])

        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "FilaMan-BambuLab-Plugin/2.6 (shop-image-metadata)",
        }
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            headers=headers,
        ) as client:
            response = await client.get(product_url)
            response.raise_for_status()
            final_url = self._allowed_shop_url(str(response.url))
            if not final_url:
                raise ValueError("Bambu shop request redirected to an unsupported host")
            if len(response.content) > SHOP_PAGE_MAX_BYTES:
                raise ValueError("Bambu shop product page exceeds the safety limit")
            images = self._extract_shop_product_images(response.text)

        self._shop_page_cache[product_url] = (now, images)
        return dict(images)

    async def _cache_shop_image_for_filament(
        self, filament_id: int
    ) -> dict[str, Any]:
        """Resolve and persist image metadata on the shared filament record."""
        async with self._shop_image_lock_for(filament_id):
            now = datetime.now(timezone.utc)
            async with async_session_maker() as db:
                filament = await db.get(Filament, filament_id)
                if filament is None:
                    return {}
                custom_fields = dict(filament.custom_fields or {})
                product_code = self._bambu_product_code(
                    custom_fields.get(ARTICLE_NUMBER_FIELD),
                    custom_fields.get(BAMBU_PRODUCT_CODE_FIELD),
                    custom_fields.get("bambu_color_code"),
                    filament.designation,
                    filament.manufacturer_color_name,
                )
                article_number_changed = False
                if product_code and not str(
                    custom_fields.get(ARTICLE_NUMBER_FIELD) or ""
                ).strip():
                    custom_fields[ARTICLE_NUMBER_FIELD] = product_code
                    filament.custom_fields = custom_fields
                    flag_modified(filament, "custom_fields")
                    article_number_changed = True
                profile_changed = self._normalize_generated_bambu_profile(
                    filament, custom_fields, product_code
                )
                if profile_changed or article_number_changed:
                    await db.commit()
                product_url = self._allowed_shop_url(
                    filament.shop_url
                    or custom_fields.get(FILAMENT_IMAGE_SOURCE_URL_FIELD)
                    or custom_fields.get(BAMBU_SHOP_SOURCE_URL_FIELD)
                ) or self._catalog_product_url(filament, custom_fields)
                image_url = self._allowed_shop_image_url(
                    custom_fields.get(FILAMENT_IMAGE_URL_FIELD)
                    or custom_fields.get(BAMBU_SHOP_IMAGE_URL_FIELD)
                )
                checked_at = self._parse_cache_timestamp(
                    custom_fields.get(FILAMENT_IMAGE_CHECKED_AT_FIELD)
                    or custom_fields.get(BAMBU_SHOP_IMAGE_CHECKED_AT_FIELD)
                )
                resolver_is_current = (
                    str(custom_fields.get(BAMBU_IMAGE_RESOLVER_VERSION_FIELD) or "")
                    == str(STORE_SEARCH_RESOLVER_VERSION)
                )
                base_metadata = {
                    "filament_id": filament.id,
                    "filament_name": filament.designation,
                    "manufacturer_color_name": filament.manufacturer_color_name,
                    "material_type": filament.material_type,
                    "bambu_product_code": product_code,
                    "shop_source_url": product_url,
                    "shop_image_url": image_url,
                    "image_provider": custom_fields.get(
                        FILAMENT_IMAGE_PROVIDER_FIELD
                    )
                    or ("bambulab" if image_url else None),
                }

            if not product_code:
                return base_metadata
            if (
                checked_at
                and now - checked_at < timedelta(days=SHOP_IMAGE_CACHE_DAYS)
                and image_url
                and resolver_is_current
            ):
                return base_metadata

            attempt_now = time.monotonic()
            _evict_expired(
                self._shop_image_last_attempt, SHOP_IMAGE_ERROR_RETRY_SECONDS, attempt_now
            )
            last_attempt = self._shop_image_last_attempt.get(filament_id, 0)
            if (
                last_attempt
                and attempt_now - last_attempt < SHOP_IMAGE_ERROR_RETRY_SECONDS
            ):
                return base_metadata
            self._shop_image_last_attempt[filament_id] = attempt_now

            search_result: dict[str, str] | None = None
            try:
                search_result = await self._fetch_store_search_image(
                    product_code, product_url
                )
            except Exception as exc:
                logger.warning(
                    "Could not resolve Bambu store search image for product %s: %s",
                    product_code,
                    exc,
                )

            search_resolved = bool(search_result)
            resolved_image = (
                search_result.get("shop_image_url") if search_result else None
            )
            if search_result:
                product_url = search_result.get("shop_source_url") or product_url

            if not resolved_image:
                resolved_image = self._optimized_shop_image_url(
                    _BAMBU_PRODUCT_IMAGES_BY_CODE.get(product_code)
                )
            if not resolved_image and product_url:
                try:
                    product_images = await self._fetch_shop_product_images(product_url)
                except Exception as exc:
                    logger.warning(
                        "Could not refresh Bambu shop image for filament %s: %s",
                        filament_id,
                        exc,
                    )
                    return base_metadata
                resolved_image = product_images.get(product_code)
            if not product_url and not resolved_image:
                return base_metadata

            base_metadata["shop_source_url"] = product_url
            checked_at_value = now.isoformat()
            async with async_session_maker() as db:
                filament = await db.get(Filament, filament_id)
                if filament is None:
                    return base_metadata
                custom_fields = dict(filament.custom_fields or {})
                custom_fields[BAMBU_SHOP_SOURCE_URL_FIELD] = product_url
                custom_fields[BAMBU_SHOP_IMAGE_CHECKED_AT_FIELD] = checked_at_value
                custom_fields[FILAMENT_IMAGE_SOURCE_URL_FIELD] = product_url
                custom_fields[FILAMENT_IMAGE_PROVIDER_FIELD] = "bambulab"
                custom_fields[FILAMENT_IMAGE_CHECKED_AT_FIELD] = checked_at_value
                if search_resolved:
                    custom_fields[BAMBU_IMAGE_RESOLVER_VERSION_FIELD] = (
                        STORE_SEARCH_RESOLVER_VERSION
                    )
                if resolved_image:
                    custom_fields[BAMBU_SHOP_IMAGE_URL_FIELD] = resolved_image
                    custom_fields[FILAMENT_IMAGE_URL_FIELD] = resolved_image
                filament.custom_fields = custom_fields
                if not filament.shop_url:
                    filament.shop_url = product_url
                flag_modified(filament, "custom_fields")
                await db.commit()

            base_metadata["shop_image_url"] = resolved_image or image_url
            base_metadata["image_provider"] = "bambulab"
            logger.info(
                "%s Bambu shop image metadata for filament %s (product %s)",
                "Cached" if resolved_image else "Checked",
                filament_id,
                product_code,
            )
            await event_bus.publish({"event": "filaments_changed"})
            return base_metadata

    def _schedule_shop_image_refresh(self, slots: list[dict[str, Any]]) -> None:
        """Queue low-frequency lookups for genuine Bambu RFID tray changes."""
        if not self._resolve_shop_images or not self._loop or not self._running:
            return
        candidates = []
        now = time.monotonic()
        _evict_expired(self._shop_slot_last_scheduled, SHOP_IMAGE_SCHEDULE_SECONDS, now)
        for slot in slots:
            if not slot.get("present"):
                continue
            # Only genuine Bambu RFID trays are allowed to trigger an external
            # shop lookup. Custom filament with matching color must not do so.
            # tray_uuid alone identifies a genuine Bambu tray (see README);
            # tag_uid is optional metadata and must not gate this.
            if not self._normalize_hex_identifier(slot.get("tray_uuid"), 32):
                continue
            identity = self._slot_identity(slot)
            last_scheduled = self._shop_slot_last_scheduled.get(identity)
            if (
                last_scheduled is not None
                and now - last_scheduled < SHOP_IMAGE_SCHEDULE_SECONDS
            ):
                continue
            self._shop_slot_last_scheduled[identity] = now
            candidates.append(dict(slot))
        if not candidates:
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._refresh_shop_images_for_slots(candidates)
            )
        )

    async def _refresh_shop_images_for_slots(
        self, slots: list[dict[str, Any]]
    ) -> None:
        """Match tray slots to filaments and cache their display metadata."""
        for slot in slots:
            slot_index = str(slot.get("slot_index") or "")
            filament_id: int | None = None
            try:
                async with async_session_maker() as db:
                    spool_id = self._slot_spool_ids.get(slot_index)
                    if spool_id:
                        spool = await db.get(Spool, spool_id)
                        filament_id = spool.filament_id if spool else None
                    if filament_id is None:
                        filament = await self._find_matching_filament(db, slot)
                        filament_id = filament.id if filament else None
                if filament_id is None:
                    continue
                metadata = await self._cache_shop_image_for_filament(filament_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Bambu shop image refresh failed for slot %s: %s", slot_index, exc
                )
                continue
            if metadata:
                self._slot_display_metadata[slot_index] = {
                    "_slot_identity": self._slot_identity(slot),
                    **metadata,
                }
