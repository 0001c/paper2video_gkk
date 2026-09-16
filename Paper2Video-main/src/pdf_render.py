"""PDF -> 图像渲染。

原实现依赖 ``pdf2image``，而 ``pdf2image`` 只是 poppler（``pdftoppm``）的包装，
Windows 上需要额外下载 poppler 二进制并配 PATH。

这里改用 **PyMuPDF**：纯 pip 安装、无需任何外部可执行文件，渲染速度还更快。
仅在 PyMuPDF 不可用时才回退到 pdf2image。
"""

import os


def render_pdf_to_images(pdf_path, dpi=400, first_page=None, last_page=None):
    """把 PDF 渲染成 PIL.Image 列表（页码从 1 开始计）。

    参数与原 ``pdf2image.convert_from_path`` 对齐，便于直接替换。
    """
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError("找不到 PDF: {}".format(pdf_path))

    try:
        import fitz  # PyMuPDF
        from PIL import Image
    except ImportError:
        print("[pdf_render] 未安装 PyMuPDF，回退到 pdf2image（需要 poppler）")
        from pdf2image import convert_from_path
        kwargs = {"dpi": dpi}
        if first_page:
            kwargs["first_page"] = first_page
        if last_page:
            kwargs["last_page"] = last_page
        return convert_from_path(pdf_path, **kwargs)

    images = []
    with fitz.open(pdf_path) as doc:
        start = (first_page - 1) if first_page else 0
        end = last_page if last_page else doc.page_count
        for page_no in range(start, min(end, doc.page_count)):
            page = doc.load_page(page_no)
            pixmap = page.get_pixmap(dpi=dpi)
            # PyMuPDF 给的是 RGB/RGBA 裸字节，转成 PIL 以兼容下游 img.save()
            mode = "RGBA" if pixmap.alpha else "RGB"
            image = Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)
            if mode == "RGBA":
                image = image.convert("RGB")
            images.append(image)

    print("[pdf_render] {} -> {} 张图 (dpi={})".format(os.path.basename(pdf_path), len(images), dpi))
    return images


def render_pdf_to_files(pdf_path, out_dir, dpi=400, prefix=""):
    """渲染并落盘，文件名从 1 开始，与 pipeline 的命名约定一致。"""
    os.makedirs(out_dir, exist_ok=True)
    images = render_pdf_to_images(pdf_path, dpi=dpi)
    paths = []
    for index, image in enumerate(images):
        out_path = os.path.join(out_dir, "{}{}.png".format(prefix, index + 1))
        image.save(out_path)
        paths.append(out_path)
    return paths
