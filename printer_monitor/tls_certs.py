"""Gestion automatique des certificats HTTPS des imprimantes (stdlib uniquement).

Complete - sans la modifier - la fonction existante
``Ctrl + clic sur IP -> webbrowser.open("https://IP")`` de app.py :
recuperation TLS en arriere-plan, analyse, suivi centralise JSON,
politique de confiance explicite SANS installation aveugle.
Aucune dependance tierce. Compatible Python 3.10.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STORE_FILE_NAME = "printer_monitor_certs.json"
DEFAULT_TLS_TIMEOUT = 3.0
DEFAULT_TLS_PORTS = (443, 8443)
WIN_STORE_ROOT = "ROOT"
WIN_STORE_CA = "CA"
WIN_STORE_MY = "MY"
ORIGIN_APP = "printer-monitor"
ORIGIN_OBSERVED = "observed"


@dataclass
class CertInfo:
    fingerprint_sha256: str = ""
    subject_cn: str = ""
    issuer_cn: str = ""
    san_list: list = field(default_factory=list)
    not_before: str = ""
    not_after: str = ""
    is_expired: bool = False
    is_self_signed: bool = False
    identity_ok: bool = False
    serial: str = ""
    der_b64_len: int = 0
    chain_len: int = 1
    pem_hash: str = ""


@dataclass
class CertDecision:
    action: str = "track"
    trusted: bool = False
    reason: str = ""
    detail: str = ""


# --- Analyse ASN.1 / DER minimale (stdlib uniquement) ---

def _der_read_tlv(data: bytes, pos: int):
    if pos >= len(data):
        raise ValueError("TLV hors limites")
    tag = data[pos]
    pos += 1
    if pos >= len(data):
        raise ValueError("TLV longueur manquante")
    first = data[pos]
    pos += 1
    if first < 0x80:
        length = first
    else:
        n = first & 0x7F
        if n == 0 or n > 4 or pos + n > len(data):
            raise ValueError("longueur DER invalide")
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    return tag, pos, length


def _der_children(data: bytes, pos: int, length: int):
    end = pos + length
    out = []
    cur = pos
    while cur < end:
        tag, cstart, clen = _der_read_tlv(data, cur)
        out.append((tag, cstart, clen))
        cur = cstart + clen
    return out


def _parse_time(s: str):
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%y%m%d%H%M%SZ", "%Y%m%d%H%M%SZ"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _ranged_now(not_before: str, not_after: str) -> bool:
    now = datetime.now(timezone.utc)
    nb = _parse_time(not_before) if not_before else None
    na = _parse_time(not_after) if not_after else None
    if nb and now < nb:
        return True
    if na and now > na:
        return True
    return False

def parse_der_identity(der: bytes) -> dict:
    result = {"subject_cn": "", "issuer_cn": "", "san": [],
              "not_before": "", "not_after": "", "serial": ""}
    try:
        tag, cstart, clen = _der_read_tlv(der, 0)
        if tag != 0x30:
            return result
        cert_children = _der_children(der, cstart, clen)
        if not cert_children:
            return result
        tbs = next((c for c in cert_children if c[0] == 0x30), cert_children[0])
        kids = _der_children(der, tbs[1], tbs[2])
        idx = 0
        if kids and kids[0][0] == 0xA0:
            idx = 1
        if idx < len(kids) and kids[idx][0] == 0x02:
            raw = der[kids[idx][1]:kids[idx][1] + kids[idx][2]]
            result["serial"] = raw.hex().upper()[:32]
            idx += 1
        if idx < len(kids) and kids[idx][0] == 0x30:
            idx += 1
        if idx < len(kids):
            result["issuer_cn"] = _name_to_cn(der, kids[idx])
            idx += 1
        if idx < len(kids) and kids[idx][0] == 0x30:
            vk = _der_children(der, kids[idx][1], kids[idx][2])
            if len(vk) >= 2:
                result["not_before"] = _time_to_text(der, vk[0])
                result["not_after"] = _time_to_text(der, vk[1])
            idx += 1
        if idx < len(kids):
            result["subject_cn"] = _name_to_cn(der, kids[idx])
            idx += 1
        for k in kids[idx:]:
            if k[0] == 0xA3:
                result["san"] = _find_san(der, k[1], k[2])
                break
    except Exception:
        pass
    return result


def _time_to_text(der: bytes, node) -> str:
    _, cstart, clen = node
    try:
        return der[cstart:cstart + clen].decode("ascii", "replace")
    except Exception:
        return ""


def _name_to_cn(der: bytes, node) -> str:
    try:
        _, cstart, clen = node
        for rdn in _der_children(der, cstart, clen):
            sets = _der_children(der, rdn[1], rdn[2]) if rdn[0] in (0x31, 0x30) else [rdn]
            for s in sets:
                atvs = _der_children(der, s[1], s[2]) if s[0] in (0x31, 0x30) else []
                if len(atvs) >= 2 and atvs[0][0] == 0x06:
                    oid = der[atvs[0][1]:atvs[0][1] + atvs[0][2]]
                    if oid == bytes((0x55, 0x04, 0x03)):
                        v = atvs[1]
                        return der[v[1]:v[1] + v[2]].decode("utf-8", "replace")
        text = der[cstart:cstart + clen].decode("utf-8", "replace")
        return "".join(ch for ch in text if ch.isprintable())[:128]
    except Exception:
        return ""


def _find_san(der: bytes, pos: int, length: int) -> list:
    sans: list = []

    def walk(p: int, end: int):
        if p + 2 > end or p + 2 > len(der):
            return
        try:
            tag, cstart, clen = _der_read_tlv(der, p)
        except Exception:
            return
        if tag == 0x30 and clen >= 0:
            kids = []
            try:
                kids = _der_children(der, cstart, clen)
            except Exception:
                kids = []
            if kids and kids[0][0] == 0x06:
                oid = der[kids[0][1]:kids[0][1] + kids[0][2]]
                if oid == bytes((0x55, 0x1D, 0x11)):
                    for k in kids[1:]:
                        if k[0] == 0x04:
                            try:
                                itag, istart, ilen = _der_read_tlv(der, k[1])
                                if itag == 0x30:
                                    for g in _der_children(der, istart, ilen):
                                        raw = der[g[1]:g[1] + g[2]]
                                        if g[0] == 0x82:
                                            try:
                                                sans.append(raw.decode("utf-8", "replace"))
                                            except Exception:
                                                pass
                                        elif g[0] == 0x87 and len(raw) == 4:
                                            sans.append(".".join(str(b) for b in raw))
                            except Exception:
                                pass
                    return
        if tag in (0x30, 0x31, 0xA0, 0xA1, 0xA2, 0xA3):
            try:
                for child in _der_children(der, cstart, clen):
                    walk(child[1] - 2 if False else _tlv_pos(der, child), child[1] + child[2])
            except Exception:
                pass
        nxt = cstart + clen
        if nxt < end:
            walk(nxt, end)

    try:
        walk(pos, pos + length)
    except Exception:
        pass
    seen = set()
    out = []
    for s in sans:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _tlv_pos(der: bytes, child) -> int:
    _, cstart, clen = child
    for back in (2, 3, 4, 5, 6):
        p = cstart - back
        if p < 0:
            continue
        try:
            t, cs, ln = _der_read_tlv(der, p)
            if cs == cstart and ln == clen:
                return p
        except Exception:
            continue
    return max(0, cstart - 2)


# --- Recuperation reseau (arriere-plan, timeouts courts) ---

def fetch_server_certificate_der(host, port=443, timeout=DEFAULT_TLS_TIMEOUT,
                                 sni_name=None):
    """Recupere le DER du certificat feuille presente par host:port.

    Handshake TLS SANS validation (contexte jetable local) : on OBSERVE le
    certificat, on ne l'approuve pas. Retourne None si injoignable.
    """
    host = (host or "").strip()
    if not host:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            with ctx.wrap_socket(sock, server_hostname=sni_name or host) as tls:
                der = tls.getpeercert(binary_form=True)
                return bytes(der) if der else None
    except Exception as exc:
        logger.debug("TLS fetch %s:%s impossible : %s", host, port, exc)
        return None


def probe_https(host, ports=DEFAULT_TLS_PORTS, timeout=DEFAULT_TLS_TIMEOUT):
    for port in ports or (443,):
        if fetch_server_certificate_der(host, port, timeout) is not None:
            return port
    return None


def build_cert_info(host: str, der: bytes) -> CertInfo:
    fp = hashlib.sha256(der).hexdigest().upper()
    pretty = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))
    ident = parse_der_identity(der)
    expired = _ranged_now(ident["not_before"], ident["not_after"])
    self_signed = bool(ident["subject_cn"] and ident["subject_cn"] == ident["issuer_cn"])
    sans = list(ident["san"] or [])
    host_l = (host or "").strip().lower()
    cn_l = (ident["subject_cn"] or "").strip().lower()
    ok = False
    if host_l:
        if host_l == cn_l:
            ok = True
        elif host_l in (s.lower() for s in sans):
            ok = True
    return CertInfo(fingerprint_sha256=pretty, subject_cn=ident["subject_cn"],
                    issuer_cn=ident["issuer_cn"], san_list=sans,
                    not_before=ident["not_before"], not_after=ident["not_after"],
                    is_expired=expired, is_self_signed=self_signed,
                    identity_ok=ok, serial=ident["serial"],
                    der_b64_len=len(der), chain_len=1, pem_hash=fp)


def der_to_pem(der: bytes) -> str:
    import base64 as _b64
    b64 = _b64.b64encode(bytes(der)).decode("ascii")
    lines = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return "-----BEGIN CERTIFICATE-----\n" + lines + "\n-----END CERTIFICATE-----\n"


def pem_to_der(pem_text: str) -> bytes:
    import base64 as _b64
    body = pem_text.split("-----BEGIN CERTIFICATE-----")[1].split("-----END CERTIFICATE-----")[0]
    return _b64.b64decode("".join(body.split()))


# --- Confiance Windows : lecture seule (certutil), ecriture isolee ---

def _silent_subprocess_kwargs():
    """Kwargs pour un subprocess totalement invisible sous Windows.

    Sans ``CREATE_NO_WINDOW``, chaque ``certutil`` lancé pendant le
    rafraîchissement (un par imprimante non fiable x 3 magasins) fait
    flasher une fenêtre CMD — comportement refusé : toutes les actions
    de fond doivent rester transparentes/invisibles.
    """
    if os.name == "nt":
        try:
            return {"creationflags": subprocess.CREATE_NO_WINDOW}
        except Exception:
            pass
    return {}


def windows_trust_probe(fingerprint_sha256: str, timeout: int = 15) -> dict:
    """Verifie (lecture seule) si l'empreinte est dans ROOT/CA/MY via certutil."""
    found = {"ROOT": False, "CA": False, "MY": False}
    if os.name != "nt":
        return found
    fp = (fingerprint_sha256 or "").replace(":", "").replace(" ", "").upper()
    if not fp:
        return found
    for store in ("ROOT", "CA", "MY"):
        try:
            proc = subprocess.run(["certutil", "-store", store],
                                  capture_output=True, text=True, timeout=timeout,
                                  **_silent_subprocess_kwargs())
            out = ((proc.stdout or "") + (proc.stderr or "")).upper()
            if fp in out.replace(" ", "").replace(":", ""):
                found[store] = True
        except Exception as exc:
            logger.debug("certutil -store %s : %s", store, exc)
    return found


def is_chain_trusted_by_windows(host, port=443, timeout=DEFAULT_TLS_TIMEOUT):
    """Handshake avec verification activee contre les CA Windows (lecture seule).

    Ne desactive jamais TLS globalement : contexte verifiant, chaine controlee.
    Pour les IP, check_hostname est desactive (EWS adressees par IP) mais la
    chaine reste verifiee contre les CA du magasin Windows.
    """
    import ipaddress as _ip
    try:
        _ip.ip_address((host or "").strip())
        is_ip = True
    except Exception:
        is_ip = False
    try:
        ctx = ssl.create_default_context()
        if is_ip:
            ctx.check_hostname = False
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            with ctx.wrap_socket(sock, server_hostname=host):
                return True, "chaine approuvee par le magasin Windows"
    except ssl.SSLCertVerificationError as exc:
        return False, "chaine non approuvee : %s" % (getattr(exc, "verify_message", None) or exc)
    except Exception as exc:
        return False, "verification impossible : %s" % exc


def install_cert_to_windows_store(pem_text: str, store: str = WIN_STORE_ROOT,
                                  timeout: int = 30):
    """Installe un PEM via certutil -addstore. OPERATION PRIVILEGIEE ISOLEE.

    Echec propre sans Windows / sans droits admin. A n'appeler que depuis la
    politique (renouvellement approuve / certificat gere verifiable) ou un
    geste explicite de l'utilisateur.
    """
    if os.name != "nt":
        return False, "installation reservee a Windows"
    if "-----BEGIN CERTIFICATE-----" not in (pem_text or ""):
        return False, "PEM invalide"
    import tempfile
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".cer", delete=False,
                                         encoding="ascii") as fh:
            fh.write(pem_text)
            tmp = fh.name
        proc = subprocess.run(["certutil", "-addstore", store, tmp],
                              capture_output=True, text=True, timeout=timeout,
                              **_silent_subprocess_kwargs())
        ok = proc.returncode == 0
        msg = ((proc.stdout or proc.stderr or "").strip()[:500])
        if not ok and ("denied" in msg.lower() or "refus" in msg.lower()):
            return False, "privileges administrateur requis (magasin %s)" % store
        return ok, msg or ("installe dans %s" % store)
    except Exception as exc:
        return False, str(exc)[:300]
    finally:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def remove_cert_from_windows_store(fingerprint_sha256: str, store: str = WIN_STORE_ROOT,
                                   timeout: int = 30):
    """Retire un certificat par empreinte. Reserve au nettoyage des GERES."""
    if os.name != "nt":
        return False, "retrait reserve a Windows"
    fp = (fingerprint_sha256 or "").replace(":", "").strip()
    if not fp:
        return False, "empreinte vide"
    try:
        proc = subprocess.run(["certutil", "-delstore", store, fp],
                              capture_output=True, text=True, timeout=timeout,
                              **_silent_subprocess_kwargs())
        ok = proc.returncode == 0
        return ok, (((proc.stdout or proc.stderr or "").strip()[:500])
                    or ("retire de %s" % store))
    except Exception as exc:
        return False, str(exc)[:300]


# --- Registre centralise persistant (observes / geres) ---

def default_store_path() -> Path:
    return Path.cwd() / STORE_FILE_NAME


class CertStore:
    """Registre JSON thread-safe : observes par imprimante, geres par l'app."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_store_path()
        self._lock = threading.Lock()
        self._data = {"printers": {}, "managed": {}, "observations": {}}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                for k in ("printers", "managed", "observations"):
                    self._data.setdefault(k, {})
        except Exception as exc:
            logger.debug("lecture registre certs impossible : %s", exc)

    def _save(self):
        try:
            self.path.write_text(json.dumps(self._data, indent=2,
                                            ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.debug("sauvegarde registre certs impossible : %s", exc)

    def record_observation(self, ip: str, info: CertInfo):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            obs = self._data["observations"].setdefault(
                info.fingerprint_sha256,
                {**asdict(info), "seen_count": 0, "first_seen": now, "last_seen": now})
            obs["seen_count"] = int(obs.get("seen_count", 0)) + 1
            obs["last_seen"] = now
            for k, v in asdict(info).items():
                obs[k] = v
            prev = self._data["printers"].get(ip, {})
            entry = {"fingerprint": info.fingerprint_sha256, "last_seen": now,
                     "first_seen": prev.get("first_seen", now),
                     "history": prev.get("history", [])}
            if prev.get("fingerprint") and prev["fingerprint"] != info.fingerprint_sha256:
                entry["history"].append({"fingerprint": prev["fingerprint"], "until": now})
                entry["history"] = entry["history"][-10:]
            self._data["printers"][ip] = entry
            self._save()

    def previous_fingerprint(self, ip: str):
        return (self._data.get("printers", {}).get(ip, {}) or {}).get("fingerprint")

    def managed_info(self, fingerprint: str):
        return (self._data.get("managed", {}) or {}).get(fingerprint)

    def mark_managed(self, info, pem_text, reason, printer_ip, store=WIN_STORE_ROOT):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            m = self._data["managed"].setdefault(info.fingerprint_sha256, {
                "origin": ORIGIN_APP, "reason": reason, "pem": pem_text,
                "store": store, "installed_at": now, "printers": []})
            m["reason"] = reason
            if pem_text and "-----BEGIN CERTIFICATE-----" in pem_text:
                m["pem"] = pem_text
            m["store"] = store
            if printer_ip not in m.get("printers", []):
                m.setdefault("printers", []).append(printer_ip)
            m["last_confirmed"] = now
            self._save()

    def unmark_managed_printer(self, fingerprint: str, ip: str):
        with self._lock:
            m = self._data["managed"].get(fingerprint)
            if not m:
                return None
            m["printers"] = [p for p in m.get("printers", []) if p != ip]
            self._save()
            return m

    def drop_managed(self, fingerprint: str):
        with self._lock:
            self._data["managed"].pop(fingerprint, None)
            self._save()

    def printers_using(self, fingerprint: str) -> list:
        out = []
        for ip, entry in (self._data.get("printers", {}) or {}).items():
            if (entry or {}).get("fingerprint") == fingerprint:
                out.append(ip)
        return out

    def forget_printer(self, ip: str):
        with self._lock:
            self._data["printers"].pop(ip, None)
            self._save()


# --- Politique de confiance (aucune approbation aveugle) ---

def decide_for_certificate(ip, info, store, windows_trusted, previous_fp,
                           config=None) -> CertDecision:
    if info.is_expired:
        return CertDecision("keep", False, "certificat expire ou hors periode",
                            "sujet=%r fin=%r" % (info.subject_cn, info.not_after))
    if not info.identity_ok:
        return CertDecision("keep", False, "identite incompatible",
                            "CN=%r SAN=%s ip=%s" % (info.subject_cn, info.san_list, ip))
    if windows_trusted:
        if previous_fp and previous_fp != info.fingerprint_sha256:
            return CertDecision("track", True, "renouvellement par autorite approuvee",
                                "%s... -> %s... issuer=%r"
                                % (previous_fp[:19], info.fingerprint_sha256[:19], info.issuer_cn))
        return CertDecision("track", True, "certificat deja fiable et valide",
                            "issuer=%r" % (info.issuer_cn,))
    managed = store.managed_info(info.fingerprint_sha256) if store else None
    if managed and managed.get("origin") == ORIGIN_APP:
        return CertDecision("track", True, "certificat gere par l'application",
                            str(managed.get("reason", ""))[:200])
    if store and previous_fp:
        prev_m = store.managed_info(previous_fp)
        if prev_m and prev_m.get("origin") == ORIGIN_APP and not info.is_self_signed:
            return CertDecision("update_managed", False, "renouvellement gere verifiable",
                                "sujet=%r issuer=%r" % (info.subject_cn, info.issuer_cn))
    if info.is_self_signed:
        return CertDecision("needs_approval", False, "autosigne inconnu : approbation requise",
                            "sujet=%r sha256=%s..." % (info.subject_cn, info.fingerprint_sha256[:27]))
    return CertDecision("needs_approval", False, "autorite inconnue : approbation requise",
                        "issuer=%r sha256=%s..." % (info.issuer_cn, info.fingerprint_sha256[:27]))


# --- Gestionnaire d'arriere-plan ---

class CertAutoManager:
    """Orchestre TLS en arriere-plan : dedie, cache, jamais bloquant SNMP."""

    def __init__(self, store=None, config=None, on_event=None):
        self.store = store or CertStore()
        self.config = dict(config or {})
        tls_cfg = self.config.get("tls", {}) or {}
        self.enabled = bool(tls_cfg.get("enabled", True))
        self.timeout = float(tls_cfg.get("timeout", DEFAULT_TLS_TIMEOUT))
        self.ports = tuple(tls_cfg.get("ports", list(DEFAULT_TLS_PORTS)))
        self.cache_ttl = int(tls_cfg.get("cache_ttl_hours", 24)) * 3600
        self.auto_install = bool(tls_cfg.get("auto_install_renewals", False))
        self._cache: dict = {}
        self._lock = threading.Lock()
        self._on_event = on_event

    def handle_printer(self, ip: str, hostname: str = "") -> dict:
        from .utils import normalize_ip, IP_RE
        ip = normalize_ip(ip) if ip else ""
        if not ip or not IP_RE.match(ip):
            return {"ip": ip, "action": "skip", "reason": "adresse non IP"}
        if not self.enabled:
            return {"ip": ip, "action": "skip", "reason": "TLS desactive"}
        now = time.time()
        with self._lock:
            cached = self._cache.get(ip)
            if cached and now - cached[0] < self.cache_ttl:
                return {**cached[1], "cached": True}
        try:
            result = self._process(ip, hostname)
        except Exception as exc:
            result = {"ip": ip, "action": "error",
                      "reason": "exception contenue : %r" % (exc,)[:200]}
        with self._lock:
            self._cache[ip] = (now, result)
        if self._on_event:
            try:
                self._on_event(result)
            except Exception:
                pass
        return result

    def cleanup_printer(self, ip: str):
        try:
            self.store.forget_printer(ip)
        except Exception:
            pass
        with self._lock:
            self._cache.pop(ip, None)

    def cleanup_managed_if_orphan(self, fingerprint: str) -> dict:
        m = self.store.managed_info(fingerprint)
        if not m or m.get("origin") != ORIGIN_APP:
            return {"removed": False, "reason": "non gere : conserve"}
        users = self.store.printers_using(fingerprint)
        if users:
            return {"removed": False, "reason": "encore sur %s : conserve" % users}
        present = windows_trust_probe(fingerprint)
        store_name = m.get("store", WIN_STORE_ROOT)
        if present.get(store_name):
            ok, msg = remove_cert_from_windows_store(fingerprint, store_name)
            if not ok:
                return {"removed": False, "reason": "retrait impossible : %s" % msg}
        self.store.drop_managed(fingerprint)
        return {"removed": True, "reason": "gere orphelin retire"}


    def handle_printers_async(self, items: list, executor=None):
        from concurrent.futures import ThreadPoolExecutor as _TPE
        own = False
        if executor is None:
            executor = _TPE(max_workers=2, thread_name_prefix="tls-certs")
            own = True
        futs = [executor.submit(self.handle_printer, ip, host or "")
                for ip, host in items]
        if own:
            def _close():
                try:
                    for f in futs:
                        try:
                            f.result(timeout=self.timeout + 5)
                        except Exception:
                            pass
                finally:
                    executor.shutdown(wait=False)
            threading.Thread(target=_close, daemon=True).start()
        return futs

    def _process(self, ip: str, hostname: str = "") -> dict:
        der = None
        used_port = None
        for port in self.ports:
            der = fetch_server_certificate_der(ip, port, self.timeout,
                                               sni_name=hostname or None)
            if der:
                used_port = port
                break
        if not der:
            return {"ip": ip, "action": "offline",
                    "reason": "pas de HTTPS joignable"}
        info = build_cert_info(hostname or ip, der)
        if not info.identity_ok and hostname:
            alt = build_cert_info(ip, der)
            if alt.identity_ok:
                info = alt
        previous_fp = self.store.previous_fingerprint(ip)
        trusted, trust_msg = is_chain_trusted_by_windows(ip, used_port or 443, self.timeout)
        if not trusted:
            probe = windows_trust_probe(info.fingerprint_sha256)
            if probe.get(WIN_STORE_ROOT) or probe.get(WIN_STORE_CA):
                trusted = True
                trust_msg = "empreinte presente dans le magasin Windows"
        decision = decide_for_certificate(ip, info, self.store, trusted, previous_fp, self.config)
        try:
            self.store.record_observation(ip, info)
        except Exception as exc:
            logger.debug("suivi certs %s : %s", ip, exc)
        auto_note = ""
        if decision.action == "update_managed" and previous_fp:
            auto_note = self._auto_rotate(ip, info, previous_fp, der)
        result = {"ip": ip, "port": used_port, "action": decision.action,
                  "trusted": decision.trusted or trusted, "reason": decision.reason,
                  "detail": decision.detail, "fingerprint": info.fingerprint_sha256,
                  "subject": info.subject_cn, "issuer": info.issuer_cn,
                  "not_after": info.not_after, "expired": info.is_expired,
                  "self_signed": info.is_self_signed, "trust_msg": trust_msg}
        if auto_note:
            result["auto"] = auto_note
        logger.info("TLS %s : %s - %s", ip, decision.action, decision.reason)
        return result

    def _auto_rotate(self, ip, info, previous_fp, der) -> str:
        prev = self.store.managed_info(previous_fp)
        if not prev or prev.get("origin") != ORIGIN_APP:
            return "rotation refusee : origine non verifiable"
        if info.is_expired or not info.identity_ok:
            return "rotation refusee : validite/identite non etablies"
        if not self.auto_install:
            try:
                self.store.unmark_managed_printer(previous_fp, ip)
            except Exception:
                pass
            return "renouvellement suivi ; install desactivee"
        pem = der_to_pem(der)
        ok, msg = install_cert_to_windows_store(pem, prev.get("store", WIN_STORE_ROOT))
        if not ok:
            return "installation impossible : %s" % msg
        try:
            self.store.mark_managed(info, pem, "renouvellement auto verifie",
                                    ip, prev.get("store", WIN_STORE_ROOT))
            self.store.unmark_managed_printer(previous_fp, ip)
        except Exception as exc:
            return "suivi partiel : %s" % exc
        clean = self.cleanup_managed_if_orphan(previous_fp)
        extra = "ancienne nettoyee" if clean.get("removed") else "ancienne conservee"
        return "renouvellement installe ; %s" % extra


# --- Approbation explicite (geste utilisateur uniquement) ---

def approve_certificate(store, ip, fingerprint, pem_text, reason,
                        store_name=WIN_STORE_ROOT):
    fp = (fingerprint or "").replace(" ", "").upper()
    if not fp or "-----BEGIN CERTIFICATE-----" not in (pem_text or ""):
        return False, "empreinte ou PEM manquant"
    if not (reason or "").strip():
        return False, "raison d'approbation requise"
    try:
        der = pem_to_der(pem_text)
    except Exception:
        return False, "PEM illisible"
    actual = ":".join("%02X" % b for b in hashlib.sha256(der).digest())
    if actual.replace(":", "") != fp.replace(":", ""):
        return False, "empreinte / PEM incoherents (refuse)"
    ok, msg = install_cert_to_windows_store(pem_text, store_name)
    if not ok:
        return False, msg
    try:
        info = build_cert_info(ip, bytes(der))
        store.mark_managed(info, pem_text, "approbation explicite : %s" % reason.strip()[:300],
                           ip, store_name)
    except Exception as exc:
        return True, "installe mais suivi partiel : %s" % exc
    return True, "approuve et installe dans %s" % store_name

    return False
