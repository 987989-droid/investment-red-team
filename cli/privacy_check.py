#!/usr/bin/env python3
"""Zero-PII Privacy Checker CLI (`privacy-check`) for Investment Red-Team.

Scans staged files, repository files, or explicit paths to ensure personal
holdings, real monetary balances, brokerage accounts, IDs, emails, phones,
secrets, or private workspace directories never leak into the public repo.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Pattern, Sequence, Tuple

IGNORE_MARKER = "privacy-check: ignore"

FORBIDDEN_PATH_PATTERNS: Tuple[Tuple[str, Pattern[str]], ...] = (
    ("private-dir-my-cases", re.compile(r"(^|/)my-cases(/|$)", re.IGNORECASE)),
    ("private-dir-portfolio", re.compile(r"(^|/)portfolio(/|$)", re.IGNORECASE)),
    ("private-dir-holdings", re.compile(r"(^|/)holdings(/|$)", re.IGNORECASE)),
    ("private-dir-reviews", re.compile(r"(^|/)reviews(/|$)", re.IGNORECASE)),
    ("env-secret-file", re.compile(r"(^|/)\.env(\..+)?$", re.IGNORECASE)),
    ("private-file-suffix", re.compile(r"\.(holdings|statement|private)\.", re.IGNORECASE)),
    ("local-secret-json", re.compile(r"\.local\.json$", re.IGNORECASE)),
)

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build"}
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".zip", ".tar", ".gz", ".xlsx", ".xls", ".pyc", ".woff", ".woff2",
}

SAFE_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "users.noreply.github.com",
    "noreply.github.com",
}

TW_ID_LETTER_MAP = {
    "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17,
    "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22, "O": 35, "P": 23,
    "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28, "V": 29, "W": 32, "X": 30,
    "Y": 31, "Z": 33,
}


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    severity: str
    message: str
    snippet: str


def is_valid_tw_national_id(candidate: str) -> bool:
    if not re.fullmatch(r"[A-Z][1289]\d{8}", candidate):
        return False
    code = TW_ID_LETTER_MAP.get(candidate[0])
    if code is None:
        return False
    digits = [code // 10, code % 10] + [int(ch) for ch in candidate[1:]]
    weights = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1]
    total = sum(d * w for d, w in zip(digits, weights))
    return total % 10 == 0


CONTENT_RULES: Tuple[Tuple[str, str, str, Pattern[str]], ...] = (
    (
        "tw-mobile-phone",
        "HIGH",
        "Detected Taiwan mobile phone number pattern",
        re.compile(r"(?<!\d)09\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)"),
    ),
    (
        "email-address",
        "HIGH",
        "Detected personal email address",
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
    (
        "labeled-account-number",
        "HIGH",
        "Detected labeled bank or brokerage account number",
        re.compile(
            r"(?:券商帳號|交割帳號|銀行帳號|帳號|account[_\s-]*(?:no|number|id))\s*[:：=]\s*['\"]?\d[\d-]{5,}",
            re.IGNORECASE,
        ),
    ),
    (
        "personal-holdings-amount",
        "HIGH",
        "Detected personal holdings balance, share count, or PnL value",
        re.compile(
            r"(?:總資產|帳戶餘額|真實持股|持有張數|持有股數|成本均價|投入本金|已實現損益|未實現損益|net[_\s-]*worth|account[_\s-]*balance)\s*[:：=]\s*[\$NTWDUS\s+-]*\d[\d,.]*",
            re.IGNORECASE,
        ),
    ),
    (
        "large-currency-amount",
        "MEDIUM",
        "Detected explicit monetary amount that may expose personal capital size",
        re.compile(
            r"(?:NT\$|TWD|USD|US\$|新台幣)\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?",
            re.IGNORECASE,
        ),
    ),
    (
        "secret-token",
        "CRITICAL",
        "Detected potential API key, token, or private key block",
        re.compile(
            r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY)"
        ),
    ),
    (
        "crypto-wallet-address",
        "HIGH",
        "Detected labeled personal crypto wallet address",
        re.compile(
            r"(?:wallet|錢包地址|冷錢包|收款地址)\s*[:：=]\s*['\"]?(?:0x[a-fA-F0-9]{40}|bc1[a-zA-Z0-9]{25,})",
            re.IGNORECASE,
        ),
    ),
)

TW_ID_REGEX = re.compile(r"\b[A-Z][1289]\d{8}\b")


def check_path_rules(rel_path: str) -> List[Finding]:
    normalized = rel_path.replace("\\", "/")
    findings: List[Finding] = []
    for rule_name, pattern in FORBIDDEN_PATH_PATTERNS:
        if pattern.search(normalized):
            findings.append(
                Finding(
                    path=normalized,
                    line=0,
                    rule=rule_name,
                    severity="CRITICAL",
                    message=f"Forbidden private path or filename detected: {normalized}",
                    snippet=normalized,
                )
            )
    return findings


def scan_text(rel_path: str, text: str) -> List[Finding]:
    findings: List[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if IGNORE_MARKER in line:
            continue

        # Check Taiwan National ID with checksum validation
        for m in TW_ID_REGEX.finditer(line):
            if is_valid_tw_national_id(m.group(0)):
                findings.append(
                    Finding(
                        path=rel_path,
                        line=lineno,
                        rule="tw-national-id",
                        severity="CRITICAL",
                        message="Detected valid Taiwan National ID number",
                        snippet=_mask_snippet(line, m.group(0)),
                    )
                )

        # Check content regex rules
        for rule_name, severity, message, pattern in CONTENT_RULES:
            for m in pattern.finditer(line):
                matched = m.group(0)
                if rule_name == "email-address":
                    domain = matched.split("@", 1)[1].lower()
                    if domain in SAFE_EMAIL_DOMAINS:
                        continue
                findings.append(
                    Finding(
                        path=rel_path,
                        line=lineno,
                        rule=rule_name,
                        severity=severity,
                        message=message,
                        snippet=_mask_snippet(line, matched),
                    )
                )
    return findings


def _mask_snippet(line: str, secret: str) -> str:
    masked = secret[:3] + "***" + secret[-2:] if len(secret) > 6 else "***"
    compact = line.strip().replace(secret, masked)
    return compact[:120]


def scan_file(file_path: Path, root: Path) -> List[Finding]:
    try:
        rel_path = file_path.relative_to(root).as_posix()
    except ValueError:
        rel_path = file_path.as_posix()

    findings = check_path_rules(rel_path)

    if file_path.suffix.lower() in BINARY_EXTENSIONS or not file_path.is_file():
        return findings

    try:
        raw = file_path.read_bytes()
    except OSError:
        return findings

    if b"\x00" in raw[:4096]:
        return findings

    text = raw.decode("utf-8", errors="replace")
    findings.extend(scan_text(rel_path, text))
    return findings


def list_staged_files(root: Path) -> List[Path]:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff --cached failed")
    return [root / line.strip() for line in proc.stdout.splitlines() if line.strip()]


def list_all_repo_files(root: Path) -> List[Path]:
    # Prefer git ls-files when inside a git repo so ignored artifacts aren't scanned accidentally,
    # while still catching untracked non-ignored files.
    if (root / ".git").exists():
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            files = [root / line.strip() for line in proc.stdout.splitlines() if line.strip()]
            return [f for f in files if f.exists()]

    collected: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            collected.append(Path(dirpath) / fname)
    return sorted(collected)


def run_scan(files: Iterable[Path], root: Path) -> List[Finding]:
    findings: List[Finding] = []
    for fpath in files:
        findings.extend(scan_file(fpath, root))
    return findings


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="irt-privacy",
        description="Zero-PII privacy scanner for Investment Red-Team public repository.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Scan all repository files (default if no paths given)")
    group.add_argument("--staged", action="store_true", help="Scan only git staged files (ideal for pre-commit)")
    parser.add_argument("paths", nargs="*", help="Explicit file or directory paths to scan")
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument("--json", action="store_true", help="Output findings as JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.staged:
        target_files = list_staged_files(root)
    elif args.paths:
        target_files = []
        for raw_p in args.paths:
            p = Path(raw_p)
            if not p.is_absolute():
                p = (root / p).resolve()
            if p.is_dir():
                target_files.extend(list_all_repo_files(p))
            else:
                target_files.append(p)
    else:
        target_files = list_all_repo_files(root)

    findings = run_scan(target_files, root)

    if args.json:
        print(json.dumps([asdict(f) for f in findings], ensure_ascii=False, indent=2))
    else:
        if not findings:
            print(f"[privacy-check] PASS: Scanned {len(target_files)} file(s), 0 privacy issues found.")
        else:
            print(f"[privacy-check] FAIL: Found {len(findings)} potential privacy leak(s):")
            for item in findings:
                loc = f"{item.path}:{item.line}" if item.line else item.path
                print(f"  - [{item.severity}] ({item.rule}) {loc} -> {item.message}")
                print(f"      snippet: {item.snippet}")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
