# poster.py
# Create a stylish, printable QR poster without external QR files.

import datetime, os, tempfile
import qrcode
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib import colors

from dotenv import load_dotenv
load_dotenv()  # add this near the imports

PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://YOUR-SUBDOMAIN.trycloudflare.com"
)
target_url = f"{PUBLIC_BASE_URL.rstrip('/')}/checkin"

# --- Resolve paths relative to THIS file (VS Code safe) ---
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
def ap(name: str) -> str:
    return os.path.join(ASSETS_DIR, name)

# === SET THIS to your public Cloudflare URL (or via .env) ===
# Your /checkin route defaults to today's session if session_id omitted.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://YOUR-SUBDOMAIN.trycloudflare.com")
target_url = f"{PUBLIC_BASE_URL.rstrip('/')}/checkin"

# Output path (into project root so it's easy to find)
pdf_path = os.path.join(BASE_DIR, f"DFC_Hangul_Checkin_QR_{datetime.date.today().isoformat()}_poster.pdf")

# Generate QR into a temp PNG
qr_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
qr_tmp_path = qr_tmp.name
qr_tmp.close()
qr_img = qrcode.make(target_url)
qr_img.save(qr_tmp_path)

# Optional stickers (PNG files) in ./assets/
FLAG_PATH    = ap("flag_korea.png")   # 🇰🇷 PNG
MASCOT_PATH  = ap("mascot.jpg")       # club mascot PNG

def safe_draw_image(c, path, x, y, w, h):
    if path and os.path.exists(path):
        try:
            c.drawImage(path, x, y, width=w, height=h, mask='auto')
        except Exception as e:
            print(f"[stickers] Failed to draw {path}: {e}")
    else:
        if path:
            print(f"[stickers] Not found: {path}")

def draw_badge_image(c, path, x, y, w, h, radius=8, pad=6):
    """Draw a light badge behind an image so it remains visible."""
    if not (path and os.path.exists(path)):  # log inside safe_draw_image
        print(f"[stickers] Not found: {path}")
        return
    # badge background
    c.setFillColorRGB(1, 1, 1)        # white fill
    c.setStrokeColorRGB(0.85, 0.85, 0.9)  # soft border
    c.setLineWidth(1.5)
    # roundRect expects lower-left origin; expand by pad
    c.roundRect(x - pad, y - pad, w + 2*pad, h + 2*pad, radius, fill=1, stroke=1)
    # image on top
    safe_draw_image(c, path, x, y, w, h)

# Build PDF
c = canvas.Canvas(pdf_path, pagesize=letter)
width, height = letter

# Header bar
c.setFillColorRGB(0.90, 0.30, 0.55)  # pink bar
c.rect(0, height - 1.35*inch, width, 1.35*inch, fill=1, stroke=0)

# Title
c.setFillColor(colors.white)
c.setFont("Helvetica-Bold", 30)
c.drawCentredString(width/2, height - 0.85*inch, "DFC Hangul — Attendance Check-In")

# Date line (below header)
c.setFillColor(colors.darkgray)
c.setFont("Helvetica-Bold", 16)
today_str = datetime.date.today().strftime("%A, %b %d, %Y")
date_y = height - 1.72*inch
c.drawCentredString(width/2, date_y, today_str)

# Move the flag BELOW the header, next to the date, on a white badge for contrast
flag_w = 0.55*inch
flag_h = 0.55*inch
# place left of the centered date text
flag_x = (width/2) - 2.6*inch
flag_y = date_y - (flag_h/2) - 3  # nudge to vertically center near date baseline
draw_badge_image(c, FLAG_PATH, flag_x, flag_y, flag_w, flag_h, radius=10, pad=6)

# Subtitle
c.setFillColor(colors.darkblue)
c.setFont("Helvetica-Oblique", 14)
c.drawCentredString(width/2, height - 2.12*inch, "Scan the QR code below to log your attendance")

# QR (medium, centered)
qr_size = 4.0 * inch
qr_x = (width - qr_size) / 2
qr_y = height/2 - qr_size/2
c.drawImage(qr_tmp_path, qr_x, qr_y, qr_size, qr_size)

# Mascot in the bottom-right corner (on-page, above the header bar)
if MASCOT_PATH:
    mascot_w = 1.2*inch
    mascot_h = 1.2*inch
    mascot_x = qr_x + qr_size + 0.4*inch   # just to the right of the QR
    mascot_y = qr_y + (qr_size/2) - (mascot_h/2)  # vertically centered next to QR
    draw_badge_image(c, MASCOT_PATH, mascot_x, mascot_y, mascot_w, mascot_h, radius=12, pad=6)


# Divider under QR
c.setStrokeColor(colors.lightgrey)
c.setLineWidth(2)
c.line(width*0.2, qr_y - 0.35*inch, width*0.8, qr_y - 0.35*inch)

# URL fallback
c.setFillColor(colors.black)
c.setFont("Helvetica", 12)
c.drawCentredString(width/2, qr_y - 0.6*inch, f"Or visit: {target_url}")

# Footer bar
c.setFillColorRGB(0.18, 0.19, 0.22)
c.rect(0, 0, width, 0.85*inch, fill=1, stroke=0)
c.setFillColor(colors.white)
c.setFont("Helvetica", 10)
c.drawCentredString(width/2, 0.45*inch, "DFC Hangul • Korean Language & Culture Club — Please keep this poster near the room entrance")

# Done
c.showPage()
c.save()

# Cleanup temp QR
try:
    os.remove(qr_tmp_path)
except Exception:
    pass

print(f"Poster saved to {os.path.abspath(pdf_path)}")
