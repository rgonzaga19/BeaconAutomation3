from datetime import datetime


def summarize_error(raw_message):
    """Turn a raw (often multi-line, technical) exception message into a
    short, single-line, non-technical remark suitable for a report table.

    API exception messages and stack traces can span many lines; displaying
    them directly as one report row's remarks breaks the table's layout.
    This only affects what appears in the report; the full untouched
    message should still reach the log file however the caller already
    logs it (e.g. logger.error(str(e))) before this runs, so no
    diagnostic detail is lost — it just isn't duplicated here.
    """
    if not raw_message:
        return "Unknown error."

    text = str(raw_message).strip()

    if not text:
        return "Unknown error."

    lowered = text.lower()

    if "timeout" in lowered or "timed out" in lowered:
        return "Beacon is not responding (request timed out)."

    if (
        "net::err" in lowered
        or "getaddrinfo" in lowered
        or "econnrefused" in lowered
        or "econnreset" in lowered
        or "internet" in lowered
    ):
        return "Internet connection issue — please check your connection."

    # Fallback for anything unrecognized: still short and non-technical,
    # but generic, since we don't know what actually broke.
    return "Beacon is unstable — check the detailed log for more information."


class ReportManager:
    def __init__(self):
        self.results = []

    def add(
        self,
        transmittal,
        status,
        mapped=0,
        remarks=""
    ):
        self.results.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "transmittal": str(transmittal),
            "status": status,
            "mapped": mapped,
            "remarks": remarks
        })

    def success(self, transmittal, mapped):
        self.add(
            transmittal=transmittal,
            status="SUCCESS",
            mapped=mapped
        )

    def skipped(self, transmittal, remarks):
        self.add(
            transmittal=transmittal,
            status="SKIPPED",
            remarks=remarks
        )

    def failed(self, transmittal, remarks):
        self.add(
            transmittal=transmittal,
            status="FAILED",
            remarks=summarize_error(remarks)
        )

    def summary(self):
        total = len(self.results)

        success = sum(
            1 for r in self.results
            if r["status"] == "SUCCESS"
        )

        skipped = sum(
            1 for r in self.results
            if r["status"] == "SKIPPED"
        )

        failed = sum(
            1 for r in self.results
            if r["status"] == "FAILED"
        )

        return {
            "total": total,
            "success": success,
            "skipped": skipped,
            "failed": failed
        }


report = ReportManager()
