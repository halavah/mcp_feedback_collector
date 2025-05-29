import io
import base64
from PIL import Image
import threading
import queue
from pathlib import Path
from datetime import datetime
import os
import sys
import argparse # 新增导入 for command-line argument parsing

from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QPushButton, QFileDialog, QMessageBox, QScrollArea, QWidget, QGroupBox,
    QSizePolicy
)
from PySide6.QtGui import QPixmap, QImage, QClipboard, QIcon, QGuiApplication
from PySide6.QtCore import Qt, Signal, Slot, QThread, QTimer, QSize, QBuffer

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image as MCPImage

# 创建MCP服务器实例
mcp = FastMCP(
    "交互式反馈收集器",
    dependencies=["pillow"]
)

# 配置默认对话框超时时间（秒）
DEFAULT_DIALOG_TIMEOUT = 300  # 5分钟
# 从环境变量读取超时时间，若未设置则使用默认值
DIALOG_TIMEOUT = int(os.getenv("MCP_DIALOG_TIMEOUT", DEFAULT_DIALOG_TIMEOUT))

# FeedbackDialog类定义，继承自QDialog，用于构建和管理反馈对话框界面
class FeedbackDialog(QDialog):
    # 用户反馈提交时发出的信号，传递反馈内容列表
    feedback_submitted = Signal(object)

    # 初始化对话框
    def __init__(self, work_summary: str = "", timeout_seconds: int = DIALOG_TIMEOUT, parent=None):
        super().__init__(parent)
        self.work_summary = work_summary
        self.timeout_seconds = timeout_seconds
        self.selected_images_data = [] # 存储选中的图片数据 (字节和原始文件名)
        self.image_preview_layout = None # 图片预览区域的布局
        self.text_widget = None # 用户文字反馈的输入框

        self.setWindowTitle("🎯 工作完成汇报与反馈收集")
        self.setGeometry(0, 0, 700, 800) # 设置初始窗口大小
        self.setMinimumSize(QSize(600, 700)) # 设置最小窗口大小
        self.setStyleSheet("""
            QDialog { background-color: #f5f5f5; }
            QLabel, QPushButton, QGroupBox, QTextEdit {
                /* A general cross-platform font stack */
                font-family: "Segoe UI", "Helvetica Neue", "Arial", "sans-serif";
            }
            /* You can add more specific fallbacks if certain scripts (e.g., CJK) are not rendering well,
               for example, by appending "Microsoft YaHei", "PingFang SC" to the font-family list above. */

            QGroupBox {
                font-size: 12pt;
                font-weight: bold;
                background-color: #ffffff;
                color: #34495e;
                border: 1px solid #dddddd;
                border-radius: 5px;
                margin-top: 1ex; /* leave space at the top for the title */
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left; /* position at the top left */
                padding: 0 3px;
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f5f5f5, stop:1 #f5f5f5);
                left: 10px; /* Adjust to align with border */
            }
            QPushButton {
                font-size: 10pt;
                font-weight: bold;
                border-radius: 3px;
                padding: 8px 12px;
                margin-top: 20px;
                min-height: 28px; /* Button height */
            }
            QPushButton:hover {
                background-color: #e0e0e0; /* Slightly darker on hover */
            }
            QTextEdit {
                /* font-family is now set globally above */
                font-size: 10pt;
                background-color: #ffffff;
                color: #2c3e50;
                border: 1px solid #cccccc;
                border-radius: 3px;
            }
        """)

        self.center_dialog() # 居中显示对话框
        self.create_widgets_pyside() # 创建对话框内的所有控件

        # 如果设置了超时，则启动超时计时器
        if self.timeout_seconds > 0:
            self.timeout_timer = QTimer(self)
            self.timeout_timer.setSingleShot(True)
            self.timeout_timer.timeout.connect(self.handle_timeout)
            self.timeout_timer.start(self.timeout_seconds * 1000)

    # 将对话框居中显示在屏幕上
    def center_dialog(self):
        screen = QGuiApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    # 处理对话框超时事件
    def handle_timeout(self):
        print("Feedback dialog timed out.")
        self.feedback_submitted.emit(None) # 发送None表示超时
        self.reject() # 关闭对话框，状态为Rejected

    # 以模态方式显示对话框，并返回用户反馈结果
    def show_dialog_pyside(self):
        result = [] # 用于存储反馈结果的列表

        # 定义一个槽函数，用于接收feedback_submitted信号发出的数据
        def on_feedback_submitted(data):
            nonlocal result
            if data is not None: # 如果不是超时
                 result.extend(data if isinstance(data, list) else [data])

        self.feedback_submitted.connect(on_feedback_submitted)

        # 执行对话框，exec_()会阻塞直到对话框关闭
        if self.exec_() == QDialog.Accepted:
            return result # 如果用户提交，则返回收集到的反馈
        else:
            return None # 如果用户取消或超时，则返回None

    # 创建并返回AI工作汇报区域的QGroupBox
    def _create_report_group(self) -> QGroupBox:
        report_groupbox = QGroupBox("📋 AI工作完成汇报")
        report_layout = QVBoxLayout(report_groupbox)
        report_layout.setContentsMargins(15,15,15,15)

        self.report_text_edit = QTextEdit()
        self.report_text_edit.setReadOnly(True)
        self.report_text_edit.setPlainText(self.work_summary or "本次对话中完成的工作内容...")
        self.report_text_edit.setFixedHeight(100)
        self.report_text_edit.setStyleSheet("background-color: #ecf0f1;")
        report_layout.addWidget(self.report_text_edit)
        return report_groupbox

    # 创建并返回用户文字反馈区域的QGroupBox
    def _create_feedback_text_group(self) -> QGroupBox:
        feedback_groupbox = QGroupBox("💬 您的文字反馈（可选）")
        feedback_layout = QVBoxLayout(feedback_groupbox)
        feedback_layout.setContentsMargins(10,10,10,10)

        self.text_widget = QTextEdit()
        self.text_widget.setPlaceholderText("请在此输入您的反馈、建议或问题...")
        self.text_widget.setMinimumHeight(100)
        self.text_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        feedback_layout.addWidget(self.text_widget)
        return feedback_groupbox

    # 创建并返回图片反馈区域的QGroupBox，包含图片选择按钮和预览区
    def _create_image_selection_group(self) -> QGroupBox:
        image_groupbox = QGroupBox("🖼️ 图片反馈（可选，支持多张）")
        image_main_layout = QVBoxLayout(image_groupbox)
        image_main_layout.setContentsMargins(10,10,10,10)
        image_main_layout.setSpacing(15)

        image_button_layout = QHBoxLayout()
        image_button_layout.setSpacing(10)

        self.select_button = QPushButton("📁 选择图片文件")
        self.select_button.setStyleSheet("background-color: #3498db; color: white;")
        self.select_button.clicked.connect(self.select_image_file_pyside)
        image_button_layout.addWidget(self.select_button)

        self.paste_button = QPushButton("📋 从剪贴板粘贴")
        self.paste_button.setStyleSheet("background-color: #2ecc71; color: white;")
        self.paste_button.clicked.connect(self.paste_from_clipboard_pyside)
        image_button_layout.addWidget(self.paste_button)

        self.clear_images_button = QPushButton("❌ 清除所有图片")
        self.clear_images_button.setStyleSheet("background-color: #e74c3c; color: white;")
        self.clear_images_button.clicked.connect(self.clear_all_images_pyside)
        image_button_layout.addWidget(self.clear_images_button)
        image_button_layout.addStretch()
        image_main_layout.addLayout(image_button_layout)

        self.preview_scroll_area = QScrollArea()
        self.preview_scroll_area.setWidgetResizable(True)
        self.preview_scroll_area.setMinimumHeight(140)
        self.preview_scroll_area.setStyleSheet("background-color: #f8f9fa; border: 1px solid #dddddd;")

        self.preview_widget = QWidget()
        self.preview_scroll_area.setWidget(self.preview_widget)
        self.image_preview_layout = QHBoxLayout(self.preview_widget)
        self.image_preview_layout.setAlignment(Qt.AlignLeft)
        self.image_preview_layout.setContentsMargins(5,5,5,5)
        self.image_preview_layout.setSpacing(10)
        self.preview_widget.setLayout(self.image_preview_layout)
        self.update_image_preview_pyside()

        image_main_layout.addWidget(self.preview_scroll_area)
        return image_groupbox

    # 创建并返回包含提交和取消按钮的底部操作按钮布局
    def _create_action_buttons_layout(self) -> QHBoxLayout:
        action_button_layout = QHBoxLayout()
        action_button_layout.setSpacing(15)
        action_button_layout.addStretch(1)

        self.submit_button = QPushButton("✅ 提交反馈")
        self.submit_button.setStyleSheet("background-color: #27ae60; color: white; font-size: 12pt; padding: 10px 15px;")
        self.submit_button.setDefault(True)
        self.submit_button.clicked.connect(self.submit_feedback_pyside)
        action_button_layout.addWidget(self.submit_button)

        self.cancel_button = QPushButton("❌ 取消")
        self.cancel_button.setStyleSheet("font-size: 12pt; padding: 10px 15px;")
        self.cancel_button.clicked.connect(self.reject)
        action_button_layout.addWidget(self.cancel_button)
        action_button_layout.addStretch(1)
        return action_button_layout

    # 主控件创建方法，调用各个辅助方法构建对话框UI
    def create_widgets_pyside(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(15)

        main_layout.addWidget(self._create_report_group())
        feedback_text_group = self._create_feedback_text_group()
        main_layout.addWidget(feedback_text_group)

        main_layout.addSpacing(10) # 在文字反馈组和图片反馈组之间添加10px的间距

        main_layout.addWidget(self._create_image_selection_group())
        main_layout.addLayout(self._create_action_buttons_layout())

        self.setLayout(main_layout)

        if self.text_widget:
            self.text_widget.setFocus() # 设置初始焦点到文字反馈输入框

    # 更新图片预览区域的显示内容
    def update_image_preview_pyside(self):
        if self.image_preview_layout is not None:
            while self.image_preview_layout.count():
                child = self.image_preview_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()

        if not self.selected_images_data:
            placeholder_label = QLabel("无图片预览。点击上方按钮添加图片。")
            placeholder_label.setAlignment(Qt.AlignCenter)
            placeholder_label.setStyleSheet("color: #888888; font-style: italic; margin: 20px;")
            self.image_preview_layout.addWidget(placeholder_label)
        else:
            for index, img_data_dict in enumerate(self.selected_images_data):
                pixmap = img_data_dict["pixmap"]

                item_widget = QWidget()
                item_layout = QVBoxLayout(item_widget)
                item_layout.setContentsMargins(5,5,5,5)
                item_layout.setSpacing(3)

                img_label = QLabel()
                scaled_pixmap = pixmap.scaled(100, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                img_label.setPixmap(scaled_pixmap)
                img_label.setAlignment(Qt.AlignCenter)
                item_layout.addWidget(img_label)

                filename = Path(img_data_dict["filename"]).name
                short_filename = filename if len(filename) < 20 else filename[:17] + "..."
                filename_label = QLabel(short_filename)
                filename_label.setAlignment(Qt.AlignCenter)
                item_layout.addWidget(filename_label)

                remove_button = QPushButton("移除")
                remove_button.setStyleSheet("font-size: 8pt; padding: 2px 5px;")
                remove_button.clicked.connect(lambda checked=False, idx=index: self.remove_image_pyside(idx))
                item_layout.addWidget(remove_button)

                self.image_preview_layout.addWidget(item_widget)

        self.image_preview_layout.addStretch()
        self.preview_widget.adjustSize()

    # 槽函数：处理用户选择图片文件的操作
    @Slot()
    def select_image_file_pyside(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片文件",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if file_paths:
            for file_path in file_paths:
                try:
                    path = Path(file_path)
                    with open(path, "rb") as f:
                        img_bytes = f.read()

                    pixmap = QPixmap()
                    pixmap.loadFromData(img_bytes)
                    if not pixmap.isNull():
                         self.selected_images_data.append({
                            "data": img_bytes,
                            "filename": path.name,
                            "pixmap": pixmap
                        })
                    else:
                        QMessageBox.warning(self, "图片加载失败", f"无法加载图片: {path.name}")
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"加载图片失败 {path.name}: {e}")
            self.update_image_preview_pyside()

    # 槽函数：处理用户从剪贴板粘贴图片的操作
    @Slot()
    def paste_from_clipboard_pyside(self):
        clipboard = QApplication.clipboard()
        mime_data = clipboard.mimeData()

        if mime_data.hasImage():
            qimage = clipboard.image()
            if not qimage.isNull():
                try:
                    byte_array = QBuffer()
                    byte_array.open(QBuffer.ReadWrite)
                    qimage.save(byte_array, "PNG")
                    img_bytes = byte_array.data().data()
                    byte_array.close()

                    pixmap = QPixmap.fromImage(qimage)

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"clipboard_image_{timestamp}.png"

                    self.selected_images_data.append({
                        "data": img_bytes,
                        "filename": filename,
                        "pixmap": pixmap
                    })
                    self.update_image_preview_pyside()
                except Exception as e:
                    QMessageBox.critical(self, "错误", f"处理剪贴板图片失败: {e}")
            else:
                QMessageBox.information(self, "无图片", "剪贴板中没有有效的图片。")
        else:
            QMessageBox.information(self, "无图片", "剪贴板中不包含图片数据。")

    # 槽函数：清除所有已选图片
    @Slot()
    def clear_all_images_pyside(self):
        if self.selected_images_data:
            reply = QMessageBox.question(self, "确认清除",
                                         "确定要清除所有已选图片吗？",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.selected_images_data.clear()
                self.update_image_preview_pyside()

    # 槽函数：移除指定索引的图片
    @Slot(int)
    def remove_image_pyside(self, index):
        if 0 <= index < len(self.selected_images_data):
            del self.selected_images_data[index]
            self.update_image_preview_pyside()

    # 槽函数：处理用户提交反馈的操作
    @Slot()
    def submit_feedback_pyside(self):
        feedback_items = []

        text_content = self.text_widget.toPlainText().strip()
        if text_content and text_content != self.text_widget.placeholderText():
            feedback_items.append(text_content)

        for img_dict in self.selected_images_data:
            try:
                pil_image = Image.open(io.BytesIO(img_dict["data"]))
                img_format = pil_image.format or "PNG"

                base64_data = base64.b64encode(img_dict["data"]).decode('utf-8')

                mcp_image_dict = {
                    "type": "image",
                    "base64_data": base64_data,
                    "format": img_format.lower(),
                    "filename": img_dict["filename"]
                }
                feedback_items.append(mcp_image_dict)
            except Exception as e:
                print(f"Error processing image {img_dict['filename']} for submission: {e}")

        self.feedback_submitted.emit(feedback_items) # 发出包含反馈内容的信号
        self.accept() # 关闭对话框，状态为Accepted


# MCP工具：收集用户反馈
@mcp.tool()
def collect_feedback(work_summary: str = "", timeout_seconds: int = DIALOG_TIMEOUT) -> list:
    """
    显示一个GUI对话框，用于收集用户的文本和图片反馈。
    Args:
        work_summary: AI完成的工作内容的汇报。
        timeout_seconds: 对话框超时时间（秒）。
    Returns:
        一个包含用户反馈的列表。每个反馈项可以是：
        - 文本字符串 (用户的文字反馈)
        - MCPImage 对象 (用户提供的图片)
        如果超时或用户取消，则返回 None 或空列表。 (当前实现：超时或取消返回空列表[])
    """
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    dialog = FeedbackDialog(work_summary=work_summary, timeout_seconds=timeout_seconds)
    feedback_data = dialog.show_dialog_pyside()

    if feedback_data is None: # 用户取消或超时 (show_dialog_pyside 返回 None)
        return [] # 根据当前设计返回空列表

    processed_feedback = []
    if not feedback_data: # 用户提交但未提供任何内容 (show_dialog_pyside 返回空列表)
        return []

    for item in feedback_data:
        if isinstance(item, str):
            processed_feedback.append(item)
        elif isinstance(item, dict) and item.get("type") == "image":
            try:
                mcp_img = MCPImage(
                    base64_data=item["base64_data"],
                    format=item["format"],
                    filename=item["filename"]
                )
                processed_feedback.append(mcp_img)
            except Exception as e:
                print(f"Error converting dictionary to MCPImage: {e}")
        else:
            print(f"Unknown item type in feedback_data: {type(item)}")

    return processed_feedback


# MCP工具：让用户选择或粘贴单个图片
@mcp.tool()
def pick_image() -> MCPImage:
    """
    弹出一个简单的GUI对话框，让用户选择单个图片文件或从剪贴板粘贴图片。
    Returns:
        MCPImage 对象，如果用户取消或没有选择图片，则可能返回 None 或引发异常（需确认MCP框架处理方式）。
        为了安全起见，如果无图片，我们将尝试返回一个空的或无效的MCPImage，或引发特定异常。
        根据原有实现，它似乎会在无图片时返回一个包含空路径的Image对象，这可能需要调整。
        FastMCP Image is: TypedDict("Image", {"format": str, "base64_data": str, "path": NotRequired[str], "url": NotRequired[str], "filename": NotRequired[str]})
    """
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    msg_box = QMessageBox()
    msg_box.setWindowTitle("选择图片来源")
    msg_box.setText("您想如何选择图片？")
    file_button = msg_box.addButton("从文件选择", QMessageBox.ActionRole)
    paste_button = msg_box.addButton("从剪贴板粘贴", QMessageBox.ActionRole)
    cancel_button = msg_box.addButton(QMessageBox.Cancel)
    msg_box.setDefaultButton(file_button)
    msg_box.exec()

    img_bytes = None
    filename = "image"
    img_format = "png"

    if msg_box.clickedButton() == file_button:
        file_path, _ = QFileDialog.getOpenFileName(
            None,
            "选择单个图片文件",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if file_path:
            path_obj = Path(file_path)
            filename = path_obj.name
            try:
                with open(path_obj, "rb") as f:
                    img_bytes = f.read()
                pil_img = Image.open(io.BytesIO(img_bytes))
                img_format = pil_img.format or "PNG"
            except Exception as e:
                QMessageBox.critical(None, "错误", f"无法加载图片 {filename}: {e}")
                return MCPImage(base64_data="", format="", filename="error_loading_file")
    elif msg_box.clickedButton() == paste_button:
        clipboard = QApplication.clipboard()
        mime_data = clipboard.mimeData()
        if mime_data.hasImage():
            qimage = clipboard.image()
            if not qimage.isNull():
                try:
                    byte_array = QBuffer()
                    byte_array.open(QBuffer.ReadWrite)
                    qimage.save(byte_array, "PNG")
                    img_bytes = byte_array.data().data()
                    byte_array.close()
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"clipboard_image_{timestamp}.png"
                    img_format = "png"
                except Exception as e:
                    QMessageBox.critical(None, "错误", f"处理剪贴板图片失败: {e}")
                    return MCPImage(base64_data="", format="", filename="error_processing_clipboard")
            else:
                QMessageBox.information(None, "无图片", "剪贴板中没有有效的图片。")
        else:
            QMessageBox.information(None, "无图片", "剪贴板中不包含图片数据。")
    elif msg_box.clickedButton() == cancel_button:
        return MCPImage(base64_data="", format="", filename="cancelled_by_user")

    if img_bytes:
        base64_data = base64.b64encode(img_bytes).decode('utf-8')
        return MCPImage(
            base64_data=base64_data,
            format=img_format.lower(),
            filename=filename
        )
    else:
        # 如果前面步骤没有成功获取图片字节且未提前返回，则执行到这里
        if msg_box.clickedButton() != cancel_button : # 避免在取消时也弹这个信息
             QMessageBox.information(None, "未选择图片", "没有选择或粘贴任何图片，或操作未成功。")
        return MCPImage(base64_data="", format="", filename="no_image_selected_or_error")


# MCP工具：获取指定路径图片的信息
@mcp.tool()
def get_image_info(image_path: str) -> str:
    """获取指定路径图片的信息（主要是尺寸和格式）"""
    try:
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        pil_img = Image.open(io.BytesIO(img_bytes))
        return f"Image format: {pil_img.format}, size: {pil_img.size}"
    except FileNotFoundError:
        return f"Error: Image file not found at {image_path}"
    except Exception as e:
        return f"Error processing image {image_path}: {e}"

# 主函数，用于启动MCP服务器或UI调试模式
def main():
    """启动MCP服务器和Qt应用（如果需要），或直接启动UI进行调试。"""
    parser = argparse.ArgumentParser(description="MCP Feedback Collector Server and UI Debug Tool")
    parser.add_argument(
        "--debug-ui",
        action="store_true",
        help="直接启动 FeedbackDialog 进行UI调试，跳过MCP服务器启动。"
    )
    parser.add_argument(
        "--summary",
        type=str,
        default="这是AI完成工作的示例汇报内容，用于UI调试。",
        help="在UI调试模式下，对话框中显示的工作汇报内容。"
    )
    args = parser.parse_args()

    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    if args.debug_ui:
        print("--- FeedbackDialog UI DEBUG MODE ---")
        # 在调试模式下，可以设置一个较短的超时时间，或者不设置超时
        # DIALOG_TIMEOUT_DEBUG = 10 # 例如10秒，或使用环境变量中的值
        print(f"启动 FeedbackDialog，汇报内容: '{args.summary}'")
        dialog = FeedbackDialog(work_summary=args.summary, timeout_seconds=DIALOG_TIMEOUT)
        feedback_result = dialog.show_dialog_pyside()
        print(f"FeedbackDialog 已关闭。返回结果: {feedback_result}")
        print("--- UI DEBUG MODE FINISHED ---")
        return # UI调试模式结束后直接退出

    # --- 以下为正常的MCP服务器启动流程 ---
    print("Starting MCP Feedback Collector Server with PySide6 GUI...")

    print(f"MCP server '{mcp.name}' with tools is configured.")
    print("Attempting to start FastMCP server...")

    try:
        # 尝试启动FastMCP服务器，具体启动方式依赖FastMCP框架的API
        if hasattr(mcp, 'run_server') and callable(mcp.run_server):
            print("Running mcp.run_server()...")
            mcp.run_server() # 假设此方法会阻塞并启动服务器
        elif hasattr(mcp, 'run') and callable(mcp.run):
            print("Running mcp.run()...")
            mcp.run() # 备选的启动方法
        else:
            print("ERROR: FastMCP object does not have a callable 'run_server' or 'run' method.")
            print("The server.py script will exit without starting a listening MCP server.")
            return

        print("FastMCP server has been shut down.")

    except KeyboardInterrupt:
        print("MCP Feedback Collector Server stopped by user (KeyboardInterrupt).")
    except Exception as e:
        print(f"An error occurred while running the MCP server: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("Main function for MCP server has finished.")

if __name__ == "__main__":
    main()