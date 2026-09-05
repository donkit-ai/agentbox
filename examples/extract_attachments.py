"""Extract CSV invoice rows from incoming attachments; no code, formulas or links are executed."""

import csv
import io
from decimal import Decimal, InvalidOperation

from _common import client, inbox_id, output


def extract_csv(content: bytes):
    if len(content) > 1_000_000:
        raise ValueError("CSV exceeds the example's 1 MB safety limit")
    rows = []
    for index, row in enumerate(csv.DictReader(io.StringIO(content.decode("utf-8-sig")))):
        if index >= 1000:
            raise ValueError("CSV exceeds 1000 rows")
        try:
            quantity, price = int(row["quantity"]), Decimal(row["unit_price"])
            if not 0 <= quantity <= 1_000_000 or not price.is_finite() or not 0 <= price <= 1_000_000:
                raise ValueError("invalid numeric range")
            rows.append(
                {"sku": row["sku"], "quantity": quantity, "unit_price": str(price), "total": str(quantity * price)}
            )
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid invoice CSV row {index + 1}") from exc
    return rows


def run():
    invoices = []
    with client() as mail:
        for message in mail.messages.iter(inbox_id(), direction="inbound", limit=20):
            for attachment in message.get("attachments", []):
                if not attachment["filename"].lower().endswith(".csv") or attachment["size_bytes"] > 1_000_000:
                    continue
                invoices.append(
                    {
                        "message_id": message["id"],
                        "attachment_id": attachment["id"],
                        "untrusted_rows": extract_csv(mail.attachments.download(attachment["id"])),
                    }
                )
    output({"invoices": invoices})


if __name__ == "__main__":
    run()
