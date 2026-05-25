import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ---------- LLM (MiniMax M2.7) ----------
MINIMAX_API_KEY = os.getenv(
    "MINIMAX_API_KEY",
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJHcm91cE5hbWUiOiJtaW5pbWF4IiwiVXNlck5hbWUiOiJtaW5pbWF4IiwiQWNjb3VudCI6IiIsIlN1YmplY3RJRCI6IjE3NTk0ODIxODAxMDAxNzAxODgiLCJQaG9uZSI6IjE3NTUxMDI3ODgzIiwiR3JvdXBJRCI6IjE3NTk0ODIxODAwOTU5NzU4ODQiLCJQYWdlTmFtZSI6IiIsIk1haWwiOiIiLCJDcmVhdGVUaW1lIjoiMjAyNS0xMi0xMCAxOToxMzo0MyIsIlRva2VuVHlwZSI6MSwiaXNzIjoibWluaW1heCJ9.vFoLedYwNyPdXmtLsU-3gMGRmCxszPIQUDrAzKFEA-WAW4yXEWlGidxDNQn2rSxD-4ex8ho6vrVu8d4Ch7_a9qepr2cXccVGlkdH0qSrsvPx59GfeulJVJc-vq9kLDpK1-3B7wBAxz9LHXObozpHZKP5OPGf6_nXq8McAKPyHiqTCdhCWJPvhceyUJtlM8q2hhqpcpoz9IJuVnsw16Sq57zrJkQxpcQLRBnDR4aiBcoglaQqnPffK9kIFv82EnU3p-6Gg7qiIjLhVPrjKkvvlPnax4mkTbjmo0PqnFIEAlzcaCKOhinLjnZrBEPJXAGJDi-idFzN4dI-LtZBsE_apQ",
)
MINIMAX_GROUP_ID = os.getenv("MINIMAX_GROUP_ID", "1759482180095975884")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M2.7")

# ---------- External PDF Parser ----------
PDF_PARSE_API_URL = "https://proddev.hailuoai.com/test/parse_pdf"

# ---------- Paths ----------
INPUT_DIR = BASE_DIR / "data" / "input"
OUTPUT_DIR = BASE_DIR / "data" / "output"
INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Image Duplicate Detection Thresholds ----------
PHASH_THRESHOLD = 8             # Hamming distance (0-64), <=8 = near-duplicate
SIFT_RATIO_THRESHOLD = 0.7     # Lowe's ratio test
SIFT_MIN_MATCHES = 25          # Minimum inlier keypoints after RANSAC
SIFT_MIN_INLIER_RATIO = 0.5   # Inliers / good_matches must exceed this
SIFT_MIN_REGION_AREA = 0.05   # Matched region must be >= 5% of image area
RANSAC_THRESHOLD = 5.0         # RANSAC reprojection error (pixels)
TEMPLATE_MATCH_THRESHOLD = 0.85 # Normalized cross-correlation
TEMPLATE_SCALES = [0.5, 0.75, 1.0, 1.25, 1.5]
MIN_IMAGE_SIZE = 50            # Skip images smaller than 50x50

# ---------- Data Anomaly Thresholds ----------
CV_THRESHOLD = 0.01            # CV < 1% is suspicious
ARITHMETIC_SEQ_TOLERANCE = 0.005  # 0.5% relative tolerance
BENFORD_MIN_SAMPLES = 100      # Minimum data points for Benford test
BENFORD_P_THRESHOLD = 0.05     # Chi-square significance level
CROSS_GROUP_OVERLAP_THRESHOLD = 0.5  # >50% overlap is suspicious
LINEAR_DEP_R2_THRESHOLD = 0.9999     # R² > 0.9999 flags near-perfect linear dependency
LINEAR_DEP_MIN_SAMPLES = 50          # Minimum data points for linear dependency check

# ---------- Cross-Sheet Detection Thresholds ----------
CROSS_SHEET_MIN_MATCHING_ROWS = 3    # Minimum matching rows to flag cross-sheet duplicate
CROSS_SHEET_COL_MATCH_RATIO = 0.9    # Column match ratio threshold (90%+ values identical)
VALUE_RECYCLING_MIN_SAMPLES = 10     # Minimum data points for value recycling check
VALUE_RECYCLING_RATIO_THRESHOLD = 0.3  # unique/total < 0.3 is suspicious

# ---------- Reference Check ----------
CROSSREF_API_BASE = "https://api.crossref.org"
CROSSREF_MAILTO = "paper-check@example.com"
REF_TITLE_SIMILARITY_THRESHOLD = 0.85
CROSSREF_RATE_LIMIT_DELAY = 0.15  # seconds between requests

# ---------- LLM Report ----------
LLM_MAX_TOKENS = 16384
LLM_TEMPERATURE = 0.3
LLM_RETRY_ATTEMPTS = 3
