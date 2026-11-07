"""数据帧解析器 frame_parser —— 程序入口。

用法：
    python main.py                 启动界面（自动载入示例预设，黑白主题）
    python main.py 抓包文件.bin     启动后直接导入该文件
    python selftest.py             无界面自检（不需要显示器，可在 CI/无桌面环境跑）
    python cli.py --help           命令行模式（不开界面）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.theme import apply_theme  # noqa: E402
from app.ui.util import ui_font  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("FrameParser")
    app.setApplicationDisplayName("数据帧解析器")
    apply_theme(app)
    app.setFont(ui_font(9))

    window = MainWindow()
    window.show()

    for candidate in argv[1:]:
        if Path(candidate).is_file():
            window.load_data_file(candidate)
            break

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
