"""
Daily Survey123 "samples expected" notifier.

Reads all configuration from .env. Checks configured ArcGIS Online survey items
for any record with Sampled == "Yes" and CreationDate on today's local date.
If any match, sends one short SMTP email. No sample list or count is included.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from arcgis.gis import GIS
from arcgis.features import FeatureLayerCollection

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "notifier_state.json"
LOG_PATH = BASE_DIR / "notifier.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("survey123_notifier")


def load_settings() -> dict:
    if not ENV_PATH.exists():
        raise FileNotFoundError(
            f"Missing {ENV_PATH}. Copy .env.example to .env and fill in the values."
        )
    load_dotenv(dotenv_path=ENV_PATH, override=False)

    required = [
        "SURVEY123_ARCGIS_USERNAME",
        "SURVEY123_ARCGIS_PASSWORD",
        "SURVEY123_SURVEYS",
        "SAMPLE_ALERT_RECIPIENTS",
        "SAMPLE_ALERT_TIMEZONE",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM",
    ]
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    if missing:
        raise ValueError("Missing required .env setting(s): " + ", ".join(missing))

    surveys = []
    # One survey per line, formatted as: Survey name|ArcGIS item ID
    for line_number, raw_line in enumerate(os.environ["SURVEY123_SURVEYS"].splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            raise ValueError(
                f"SURVEY123_SURVEYS line {line_number} must use 'Survey name|item ID' format. Found {line!r}."
            )
        name, item_id = (part.strip() for part in line.split("|", 1))
        if not item_id:
            raise ValueError(f"SURVEY123_SURVEYS line {line_number} has no item ID.")
        surveys.append({"name": name or item_id, "item_id": item_id})
    if not surveys:
        raise ValueError("Add at least one survey to SURVEY123_SURVEYS in .env.")
        
    recipients = []  
    # One survey per line, formatted as: Survey name|ArcGIS item ID
    for contact in os.environ["SAMPLE_ALERT_RECIPIENTS"].split(";"):
        recipients.append(contact.strip())

    if not recipients:
        raise ValueError("Add at least one recipient to SAMPLE_ALERT_RECIPIENTS in .env.")

    try:
        port = int(os.environ["SMTP_PORT"])
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be a number, usually 587 or 465.") from exc

    return {
        "arcgis_username": os.environ["SURVEY123_ARCGIS_USERNAME"].strip(),
        "arcgis_password": os.environ["SURVEY123_ARCGIS_PASSWORD"],
        "arcgis_url": os.environ["SURVEY123_ARCGIS_URL"],
        "surveys": surveys,
        "recipients": recipients,
        "timezone": os.environ["SAMPLE_ALERT_TIMEZONE"].strip(),
        "smtp_host": os.environ["SMTP_HOST"].strip(),
        "smtp_port": port,
        "smtp_username": os.environ["SMTP_USERNAME"].strip(),
        "smtp_password": os.environ["SMTP_PASSWORD"],
        "smtp_from": os.environ["SMTP_FROM"].strip(),
        "smtp_starttls": os.environ.get("SMTP_STARTTLS", "true").strip().lower() in {"1", "true", "yes"},
        "smtp_ssl": os.environ.get("SMTP_SSL", "false").strip().lower() in {"1", "true", "yes"},
    }


def load_last_email_date() -> str | None:
    if not STATE_PATH.exists():
        return None
    try:
        return STATE_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        log.warning("Could not read state file; proceeding without duplicate protection.")
        return None


def save_last_email_date(day: str) -> None:
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(day, encoding="utf-8")
    temp_path.replace(STATE_PATH)


def is_today(epoch_ms, tz: ZoneInfo, local_today) -> bool:
    try:
        dt = datetime.fromtimestamp(float(epoch_ms) / 1000, tz=timezone.utc).astimezone(tz)
        return dt.date() == local_today
    except (TypeError, ValueError, OSError):
        return False


def get_layers(gis: GIS, item_id: str, label: str):
    item = gis.content.get(item_id)
    if item is None:
        raise RuntimeError(
            f"Cannot access ArcGIS item {item_id!r} ({label}). Check the item ID and service-account permissions."
        )

    collection = None
    try:
        collection = FeatureLayerCollection.fromitem(item)
    except Exception:
        pass

    if collection is None:
        related = item.related_items("Survey2Service", "forward")
        if not related:
            related = item.related_items("Survey2Data", "forward")
        if related:
            collection = FeatureLayerCollection.fromitem(related[0])

    if collection is None:
        raise RuntimeError(
            f"Could not resolve a feature service for {label} ({item_id}). "
            "Try the associated hosted feature-layer item ID."
        )
    return collection.layers


def survey_has_samples_today(gis: GIS, survey: dict, tz: ZoneInfo, local_today) -> bool:
    item_id = survey["item_id"]
    label = survey["name"]
    layers = get_layers(gis, item_id, label)

    for layer_index, layer in enumerate(layers):
        fields = {f["name"].lower(): f["name"] for f in layer.properties.fields}
        if "sampled" not in fields or "creationdate" not in fields:
            raise RuntimeError(
                f"Layer {layer_index} of {label} ({item_id}) must have Sampled and CreationDate fields. "
                f"Available fields: {', '.join(fields.values())}"
            )

        sampled_field = fields["sampled"]
        creation_field = fields["creationdate"]
        # Query only affirmative records; compare CreationDate in local time below.
        result = layer.query(
            where=f"UPPER({sampled_field}) = 'YES'",
            out_fields=creation_field,
            return_geometry=False,
            return_all_records=True,
        )
        for feature in result.features:
            if is_today(feature.attributes.get(creation_field), tz, local_today):
                log.info("Found at least one sample collected today in survey '%s'.", label)
                return True

    log.info("No qualifying samples found in survey '%s'.", label)
    return False


def send_smtp_alert(settings: dict, local_today) -> None:
    for recipient in settings["recipients"]:
        msg = EmailMessage()
        # msg["Subject"] = f"HORIZONS SAMPLE NOTIFICATION: "
        msg["Subject"] = f"TEST PLEASE IGNORE TEST: "
        msg["From"] = settings["smtp_from"]
        msg["To"] = recipient
        msg.set_content(
            f"Samples were recorded as collected today ({local_today:%A, %d %B %Y}).\n\n"
            "Please send a courier to pick up these samples today. Thank you.\n"
        )

        context = ssl.create_default_context()
        if settings["smtp_ssl"]:
            with smtplib.SMTP_SSL(
                settings["smtp_host"], settings["smtp_port"], context=context, timeout=30
            ) as server:
                server.login(settings["smtp_username"], settings["smtp_password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(settings["smtp_host"], settings["smtp_port"], timeout=30) as server:
                server.ehlo()
                if settings["smtp_starttls"]:
                    server.starttls(context=context)
                    server.ehlo()
                server.login(settings["smtp_username"], settings["smtp_password"])
                server.send_message(msg)

        log.info("Sent samples-expected SMTP alert to %s.", recipient)


def main() -> int:
    settings = load_settings()
    log.info(f"Found recipients:{settings['recipients']}")
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz)
    # now = datetime(year=2026, month=10, day=15, hour=13)
    today = now.date()
    today_str = today.isoformat()

    if load_last_email_date() == today_str:
        log.info("Notification already sent for %s; skipping duplicate.", today_str)
        return 0

    log.info("Checking whether any samples were collected on %s.", today_str)
    gis = GIS(settings["arcgis_url"], settings["arcgis_username"], settings["arcgis_password"])

    any_samples_today = False
    # A query failure raises an exception, rather than falsely reporting no samples.
    for survey in settings["surveys"]:
        log.info(f"Checking {survey["name"]}...")
        if survey_has_samples_today(gis, survey, tz, today):
            log.info(f"Samples collected today for {survey["name"]}!")
            any_samples_today = True
            break
        else:
            log.info(f"No samples.")

    if not any_samples_today:
        log.info("No samples found for today's local date. No email sent.")
        return 0

    # Store date only after SMTP accepts the message, allowing retry after send failure.
    send_smtp_alert(settings, today)
    save_last_email_date(today_str)
    log.info("Completed successfully.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log.exception("Notifier run failed.")
        raise
