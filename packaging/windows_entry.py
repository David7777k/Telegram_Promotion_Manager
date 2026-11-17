"""Entry point for the Windows distribution."""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from promotion_studio.__main__ import main


def launch():
    if sys.argv[1:] == ["--package-smoke"]:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from promotion_studio.desktop_service import DesktopService
        from promotion_studio.telegram_accounts import _client
        import qrcode

        ZoneInfo("Europe/Kyiv")
        _client(1, "0" * 32)
        qr = qrcode.QRCode()
        qr.add_data("tg://login?token=local-smoke")
        if not qr.get_matrix():
            raise RuntimeError("The QR renderer is unavailable")
        with TemporaryDirectory(prefix="Studio-проверка-") as folder:
            profile = Path(folder)
            service = DesktopService(profile)
            service.initialize()
            if service.status() != {"jobs": [], "subscriptions": []}:
                raise RuntimeError("Clean packaged profile is not empty")
            app = QApplication([])
            QTimer.singleShot(200, app.quit)
            sys.argv = [sys.argv[0], "--desktop", "--profile", folder]
            try:
                main()
            except SystemExit as result:
                if result.code != 0:
                    raise
            print("Packaged Windows Qt, SQLite, Telethon, QR and tzdata smoke OK")
        return
    if len(sys.argv) == 1:
        local = os.environ.get("LOCALAPPDATA")
        profile = Path(local) / "TelegramPromotionStudio" if local else Path.home() / "TelegramPromotionStudio"
        sys.argv.extend(["--desktop", "--profile", str(profile)])
    main()


if __name__ == "__main__":
    launch()

