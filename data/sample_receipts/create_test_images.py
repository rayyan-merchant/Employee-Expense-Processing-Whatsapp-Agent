"""Run once: python data/sample_receipts/create_test_images.py"""
import os

from PIL import Image, ImageDraw


def create():
    os.makedirs("data/sample_receipts", exist_ok=True)
    img = Image.new("RGB", (400, 600), "white")
    draw = ImageDraw.Draw(img)
    lines = [
        "CAFE AROMA",
        "123 Dizengoff St, Tel Aviv",
        "Date: 20/05/2024",
        "----------------------------",
        "2x Coffee         30.00",
        "1x Sandwich       45.00",
        "----------------------------",
        "Subtotal:         75.00",
        "VAT (17%):        12.75",
        "TOTAL:            87.75 NIS",
        "Thank you for visiting!",
    ]
    y = 40
    for line in lines:
        draw.text((30, y), line, fill="black")
        y += 40
    img.save("data/sample_receipts/test_receipt_en.jpg", "JPEG")

    img2 = Image.new("RGB", (400, 600), "white")
    draw2 = ImageDraw.Draw(img2)
    for line in ["Chef Restaurant", "Herzl 45 Tel Aviv", "Date: 20/05/2024", "Total: 250.00 NIS"]:
        draw2.text((30, y), line, fill="black")
        y += 40
    img2.save("data/sample_receipts/test_receipt_he.jpg", "JPEG")
    print("Test images created.")


if __name__ == "__main__":
    create()
