#!/usr/bin/env python3
"""
fidelidade_scraper.py — Importa apólices do portal MyFidelidade (www.my.fidelidade.pt).

Usage:
  python3 bin/fidelidade_scraper.py [--dry-run] [--safe] [--debug]

Flags:
  --dry-run   Não escreve na BD; mostra o que seria feito
  --safe      Pára imediatamente se login falhar (não continua com dados parciais)
  --debug     Screenshots + logs detalhados

Env vars (/etc/aios.env):
  FIDELIDADE_NIF   — NIF de acesso
  FIDELIDADE_PASS  — Palavra-passe
"""
import sys, os, time, json, re, argparse, traceback
from datetime import datetime, date

_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _l in open(_env_file):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _, _v = _l.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

import sqlalchemy as sa

DATABASE_URL  = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
NIF           = os.environ.get("FIDELIDADE_NIF", "")
PASS          = os.environ.get("FIDELIDADE_PASS", "")
PERSONAL_NIF  = os.environ.get("FIDELIDADE_PERSONAL_NIF", NIF)  # para validação 2FA empresa

LOG_FILE   = "/home/jdl/ai-os/runtime/logs/fidelidade_scraper.log"
PORTAL_URL = "https://www.my.fidelidade.pt"
APOLICES_URL = "https://www.my.fidelidade.pt/CAND_AC_Apolices/"
ENTRAR_ID = (
    "SingleSignOn_Th_wtwb_layout_block_wtContentRight_SSO_Login_CW_wtwb_Login_block"
    "_OutSystemsUIWeb_wtloading_block_wtButton_wtlnk_Login"
)

# ── Empresa portal constants ────────────────────────────────────────────────────
EMPRESA_DISAMBIGUATOR  = "https://www.empresasmy.fidelidade.pt/MFEF_MyFidelidadeEmpresas/Disambiguator"
EMPRESA_POLICIES_URL   = "https://www.empresasmy.fidelidade.pt/MFEF_MyFidelidadeEmpresas/PoliciesList"
EMPRESA_NIF_ID = (
    "SingleSignOn_Th_wtwb_newLayout_block_wtContentRight_SSO_LoginOtherEcosystems_CW_wtwb_CompanyLogin2_block"
    "_SingleSignOn_Pat_wtwb_InputField_Username_block_wtInput_SingleSignOn_Pat_wtwb_input_status_username_block"
    "_wtInput_wtinp_username"
)
EMPRESA_PASS_ID = (
    "SingleSignOn_Th_wtwb_newLayout_block_wtContentRight_SSO_LoginOtherEcosystems_CW_wtwb_CompanyLogin2_block"
    "_SingleSignOn_Pat_wtwb_input_password_block_wtInput_SingleSignOn_Pat_wtwb_input_status_block"
    "_wtInput_wtinp_password"
)
EMPRESA_ENTRAR_ID = (
    "SingleSignOn_Th_wtwb_newLayout_block_wtContentRight_SSO_LoginOtherEcosystems_CW_wtwb_CompanyLogin2_block"
    "_OutSystemsUIWeb_wtloading_block_wtButton_wtlnk_Login"
)

CATEGORY_MAP = {
    "liber 3g":         "Automóvel",
    "auto":             "Automóvel",
    "hi0":              "Vida",
    "vida":             "Vida",
    "multirriscos":     "Multirriscos",
    "habitação":        "Habitação",
    "saúde":            "Saúde",
    "multicare":        "Saúde",
    "acidentes":        "Acidentes Pessoais",
    "responsabilidade": "Responsabilidade Civil",
    "acidentes trabalho": "Acidentes de Trabalho",
    "at-trabalhador":     "Acidentes de Trabalho",
    "trabalhador conta":  "Acidentes de Trabalho",
}

# ── Logger ─────────────────────────────────────────────────────────────────────

class RunLog:
    """Accumulates structured log entries and writes to file + stdout."""

    def __init__(self, dry_run: bool):
        self.dry_run   = dry_run
        self.started   = datetime.utcnow()
        self.entries: list[dict] = []
        self._ensure_log_dir()

    def _ensure_log_dir(self):
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    def _now(self) -> str:
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _add(self, level: str, msg: str, **extra):
        entry = {"ts": self._now(), "level": level, "msg": msg, **extra}
        self.entries.append(entry)
        return entry

    # ── Public helpers ──

    def info(self, msg: str, **kw):
        self._add("INFO", msg, **kw)
        print(f"  ✓ {msg}")

    def warn(self, msg: str, **kw):
        self._add("WARN", msg, **kw)
        print(f"  ⚠ {msg}")

    def error(self, msg: str, **kw):
        self._add("ERROR", msg, **kw)
        print(f"  ✗ {msg}", file=sys.stderr)

    def debug(self, msg: str, **kw):
        # Debug entries go to log file only (unless caller prints them)
        self._add("DEBUG", msg, **kw)

    def flush(self, result: dict):
        """Write complete run record to log file."""
        ended = datetime.utcnow()
        record = {
            "run_start":  self.started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_end":    ended.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": round((ended - self.started).total_seconds(), 1),
            "dry_run":    self.dry_run,
            "result":     result,
            "entries":    self.entries,
        }
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            print(f"  ⚠ Não foi possível escrever log: {e}", file=sys.stderr)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _infer_category(name: str) -> str:
    n = name.lower()
    for k, v in CATEGORY_MAP.items():
        if k in n:
            return v
    return name.strip().title()


def _parse_date(s: str):
    if not s:
        return None
    s = s.strip()
    if s.startswith("1900") or s.startswith("9999"):
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _parse_amount(s: str):
    if not s:
        return None
    s = re.sub(r"[^\d,.]", "", s).replace(",", ".")
    parts = s.rsplit(".", 1)
    if len(parts) == 2:
        s = parts[0].replace(".", "") + "." + parts[1]
    try:
        return float(s) if s else None
    except ValueError:
        return None


# ── Login ──────────────────────────────────────────────────────────────────────

def login(page, log: RunLog, debug: bool, nif: str = None, password: str = None, personal_nif: str = None) -> bool:
    _nif  = nif or NIF
    _pass = password or PASS
    _pnif = personal_nif or PERSONAL_NIF

    if not _nif or not _pass:
        log.error("NIF / PASS não definidos")
        return False

    try:
        page.goto(PORTAL_URL, timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception as e:
        log.error(f"Falha ao carregar portal: {e}")
        return False

    try:
        page.click("#username")
        time.sleep(0.5)
        page.keyboard.type(_nif, delay=80)
        time.sleep(0.3)
        page.keyboard.press("Tab")
        time.sleep(0.3)
        page.keyboard.type(_pass, delay=80)
        time.sleep(0.5)
        page.click(f"#{ENTRAR_ID}", timeout=10_000)
    except Exception as e:
        log.error(f"Falha ao interagir com formulário de login: {e}")
        return False

    # Poll up to 60s — pode pedir NIF pessoal de validação (conta empresa)
    for elapsed in range(60):
        time.sleep(1)
        try:
            # Verificar se pede validação de utilizador (NIF pessoal)
            inp = page.query_selector('input[id*="NIF"], input[placeholder*="NIF"], input[name*="nif"]')
            if inp and _pnif and _pnif != _nif:
                log.debug(f"Pedido de validação de utilizador após {elapsed+1}s — a preencher NIF pessoal")
                inp.click()
                time.sleep(0.3)
                page.keyboard.type(_pnif, delay=80)
                time.sleep(0.3)
                # Submeter
                btn = page.query_selector('button[type="submit"], input[type="submit"]')
                if btn:
                    btn.click()
                    time.sleep(2)

            el = page.query_selector('a:has-text("meus seguros"), a:has-text("seguros")')
            if el:
                log.debug(f"Portal detectado após {elapsed+1}s (URL={page.url[:80]})")
                time.sleep(2)
                return True
        except Exception:
            pass

    if debug:
        page.screenshot(path="/tmp/fidelidade_login_fail.png")
    log.error(f"Portal não detectado após 60s (URL={page.url[:80]})")
    return False


# ── Empresa login ──────────────────────────────────────────────────────────────

def empresa_login(page, log: RunLog, debug: bool, nif: str = None, password: str = None) -> bool:
    """Login to empresasmy.fidelidade.pt using personal NIF + password (same NIF both steps)."""
    _nif  = nif or NIF
    _pass = password or PASS

    if not _nif or not _pass:
        log.error("NIF / PASS não definidos para empresa")
        return False

    try:
        page.goto(EMPRESA_DISAMBIGUATOR, timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception as e:
        log.error(f"Falha ao carregar portal empresa: {e}")
        return False

    # Step 1: fill personal NIF on Disambiguator
    try:
        page.click("#Input_VatNumber")
        time.sleep(0.3)
        page.keyboard.type(_nif, delay=80)
        time.sleep(0.3)
        btn = page.query_selector('button:has-text("AVANÇAR")')
        if not btn:
            log.error("Botão AVANÇAR não encontrado no Disambiguator")
            return False
        btn.click(force=True)
        time.sleep(3)
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception as e:
        log.error(f"Falha no Disambiguator: {e}")
        return False

    # Step 2: fill NIF + password on empresa login form
    try:
        inp = page.query_selector(f"#{EMPRESA_NIF_ID}")
        if not inp:
            log.error("Campo NIF da empresa não encontrado")
            if debug:
                page.screenshot(path="/tmp/empresa_login_fail.png")
            return False
        inp.click()
        time.sleep(0.3)
        page.keyboard.type(_nif, delay=120)
        time.sleep(0.5)

        pinp = page.query_selector(f"#{EMPRESA_PASS_ID}")
        if not pinp:
            log.error("Campo password da empresa não encontrado")
            return False
        pinp.click()
        time.sleep(0.2)
        page.keyboard.type(_pass, delay=80)
        time.sleep(0.5)

        entrar = page.query_selector(f"#{EMPRESA_ENTRAR_ID}")
        if not entrar:
            log.error("Botão ENTRAR da empresa não encontrado")
            return False
        entrar.click(force=True)
    except Exception as e:
        log.error(f"Falha ao preencher formulário empresa: {e}")
        return False

    # Poll up to 30s for successful login
    for elapsed in range(30):
        time.sleep(1)
        try:
            txt = page.inner_text("body")
            if any(x in txt for x in ["Apólices", "Apolices", "JOAO DIOGO", "Bem vindo", "Sair"]):
                log.debug(f"Portal empresa detectado após {elapsed+1}s")
                time.sleep(1)
                return True
            if "credenciais" in txt.lower() or "não válid" in txt.lower():
                log.error(f"Credenciais empresa inválidas (URL={page.url[:80]})")
                return False
        except Exception:
            pass

    if debug:
        page.screenshot(path="/tmp/empresa_login_fail.png")
    log.error(f"Portal empresa não detectado após 30s (URL={page.url[:80]})")
    return False


# ── Parse empresa detail page ──────────────────────────────────────────────────

def _parse_empresa_page(text: str, apolice_num: str) -> dict:
    """Parse a full empresa policy page (all tabs visible in one inner_text call)."""

    # Product name: line after "PRODUTO"
    product_name = ""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() == "PRODUTO" and i + 1 < len(lines):
            product_name = lines[i + 1].strip()
            break

    result = {
        "entity_type":      "company",
        "entity_ref":       "JOAO DIOGO LOPES UNIP LDA",
        "insurer_name":     "Fidelidade",
        "policy_number":    apolice_num,
        "category":         _infer_category(product_name),
        "coverage_summary": product_name,
        "auto_renew":       True,
        "status":           "active",
        "notes":            f"Importado de MyFidelidade Empresas — {product_name}",
    }

    def _labeled(label, txt):
        m = re.search(rf"{re.escape(label)}\s*[:\n]\s*(\S[^\n]*)", txt)
        return m.group(1).strip() if m else None

    # Dates
    def _dt_after(label, txt):
        m = re.search(rf"{re.escape(label)}\s*[:\n]\s*(\d{{2}}-\d{{2}}-\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})", txt)
        return _parse_date(m.group(1)) if m else None

    result["start_date"]   = _dt_after("Data de início", text)
    result["end_date"]     = _dt_after("Data de termo/renovação", text)
    result["renewal_date"] = result["end_date"]

    # Premium — handles "1 158,22 €", "1\xa0158,22 €" and "108,80€"
    m = re.search(r"Pr.mio anual.*?([0-9][0-9 \xa0.,]+)\s*€", text, re.IGNORECASE | re.DOTALL)
    if m:
        result["premium_amount"] = _parse_amount(m.group(1))

    # Payment frequency
    freq = _labeled("Periodicidade", text)
    if freq:
        result["payment_frequency"] = freq[:50]

    # For fleet auto policies, record all plates in notes
    plates = re.findall(r'\b([0-9]{2}-[A-Z]{2}-[0-9]{2}|[A-Z]{2}-[0-9]{2}-[A-Z]{2}|[0-9]{2}-[A-Z]{2}-[A-Z]{2})\b', text)
    if plates:
        unique_plates = list(dict.fromkeys(plates))  # preserve order, dedup
        result["notes"] += f" | Viaturas: {', '.join(unique_plates)}"
        # For single-vehicle policies, set vehicle fields
        if len(unique_plates) == 1:
            result["vehicle_matricula"] = unique_plates[0]
            result["entity_type"] = "vehicle"
            result["entity_ref"]  = unique_plates[0]
            # Try to extract marca/modelo near the plate
            m = re.search(rf"{re.escape(unique_plates[0])}\s+([A-Z][A-Za-z0-9 ]+?)\s+\d{{2}}-\d{{2}}-\d{{4}}", text)
            if m:
                parts = m.group(1).strip().split()
                if len(parts) >= 1:
                    result["vehicle_marca"] = parts[0]
                if len(parts) >= 2:
                    result["vehicle_modelo"] = " ".join(parts[1:])

    return result


# ── Scrape empresa policies ────────────────────────────────────────────────────

def scrape_empresa_policies(page, log: RunLog, debug: bool) -> list[dict]:
    try:
        page.goto(EMPRESA_POLICIES_URL, timeout=20_000)
        time.sleep(4)
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception as e:
        log.error(f"Falha ao carregar lista de apólices empresa: {e}")
        return []

    if debug:
        page.screenshot(path="/tmp/empresa_apolices.png")

    # Find all policy number links (href='#')
    # Policy numbers appear as links inside table rows
    pol_rows = page.query_selector_all('tr')
    pol_nums = []
    for row in pol_rows:
        links = row.query_selector_all('a[href="#"]')
        for lnk in links:
            txt = lnk.inner_text().strip()
            # Policy numbers: all digits or AT+digits
            if re.match(r'^[A-Z]{0,3}\d{6,}$', txt):
                pol_nums.append(txt)

    n_found = len(pol_nums)
    log.debug(f"Lista empresa: {n_found} apólice(s) encontrada(s): {pol_nums}")

    if n_found == 0:
        log.warn("Nenhuma apólice encontrada no portal empresa")
        return []

    policies = []

    for i, pol_num in enumerate(pol_nums):
        log.debug(f"Apólice empresa {i+1}/{n_found}: {pol_num}")

        # Always navigate back to list (ensures clean SPA state)
        try:
            page.goto(EMPRESA_POLICIES_URL, timeout=20_000)
            time.sleep(4)
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception as e:
            log.warn(f"Apólice {pol_num}: falha ao carregar lista — {e}")
            continue

        # Click the policy link
        lnk = page.query_selector(f'a:has-text("{pol_num}")')
        if not lnk:
            log.warn(f"Apólice {pol_num}: link não encontrado na lista")
            continue

        try:
            lnk.click()
            time.sleep(5)
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception as e:
            log.warn(f"Apólice {pol_num}: falha ao abrir — {e}")
            continue

        # Click "Dados Gerais" tab and wait for data to appear
        for _attempt in range(3):
            try:
                tab = page.query_selector('a:has-text("Dados Gerais"), button:has-text("Dados Gerais")')
                if tab:
                    tab.click()
                    time.sleep(3)
                    # Verify data loaded
                    if "Data de início" in page.inner_text("body"):
                        break
                    time.sleep(2)
            except Exception:
                pass

        detail_text = page.inner_text("body")

        if debug:
            page.screenshot(path=f"/tmp/empresa_pol_{pol_num}.png")

        policy = _parse_empresa_page(detail_text, pol_num)
        policies.append(policy)
        log.debug(
            f"  -> {policy['category']} end={policy.get('end_date')} "
            f"premium={policy.get('premium_amount')}€"
        )

    return policies


# ── Parse detail page ─────────────────────────────────────────────────────────

def _parse_detail_page(text: str, product_name: str, apolice_num: str) -> dict:
    result = {
        "entity_type":      "particular",
        "entity_ref":       f"NIF {NIF}",
        "insurer_name":     "Fidelidade",
        "policy_number":    apolice_num,
        "category":         _infer_category(product_name),
        "coverage_summary": product_name,
        "auto_renew":       True,
        "status":           "active",
        "notes":            f"Importado de MyFidelidade — {product_name}",
    }

    def _labeled_date(label, txt):
        m = re.search(rf"{re.escape(label)}\s+(\d{{4}}-\d{{2}}-\d{{2}}|\d{{2}}/\d{{2}}/\d{{4}})", txt)
        return _parse_date(m.group(1)) if m else None

    result["start_date"]   = _labeled_date("Data início", text) or _labeled_date("Data Início", text)
    result["end_date"]     = _labeled_date("Data renovação/termo", text) or \
                              _labeled_date("Data Renovação/Termo", text)
    result["renewal_date"] = result["end_date"]

    m = re.search(r"De\s+(\d{4}-\d{2}-\d{2})\s+A\s+(\d{4}-\d{2}-\d{2})", text)
    if m:
        if not result["start_date"]:
            result["start_date"] = _parse_date(m.group(1))
        if not result["end_date"]:
            result["end_date"] = _parse_date(m.group(2))

    m = re.search(r"Prémio\s+(?:anual|único)[^\d]+([\d.,]+)\s*€", text, re.IGNORECASE)
    if m:
        result["premium_amount"] = _parse_amount(m.group(1))

    m = re.search(r"Periodicidade de pagamento\s+(.+?)(?:\n|Forma)", text, re.DOTALL)
    if m:
        result["payment_frequency"] = m.group(1).strip()[:50]

    m = re.search(r"Tomador do seguro\s+(.+?)(?:\n|Morada)", text, re.DOTALL)
    if m:
        tomador = m.group(1).strip()
        if "Silva" in tomador or "João" in tomador or "Joao" in tomador:
            result["entity_ref"] = "Joao Diogo Lopes Silva"

    # Vehicle fields (only present for auto policies)
    m = re.search(r"Matr[íi]cula\s+([A-Z0-9]{2}[-\s]?[A-Z0-9]{2}[-\s]?[A-Z0-9]{2})", text, re.IGNORECASE)
    if m:
        mat = re.sub(r"\s+", "-", m.group(1).strip().upper())
        result["vehicle_matricula"] = mat
        result["entity_type"] = "vehicle"
        result["entity_ref"]  = mat

    m = re.search(r"Marca\s+([^\n]+)", text, re.IGNORECASE)
    if m:
        result["vehicle_marca"] = m.group(1).strip()[:60]

    m = re.search(r"Modelo\s+([^\n]+)", text, re.IGNORECASE)
    if m:
        result["vehicle_modelo"] = m.group(1).strip()[:60]

    return result


# ── Scrape policies ───────────────────────────────────────────────────────────

def scrape_policies(page, log: RunLog, debug: bool) -> list[dict]:
    try:
        page.click('a:has-text("meus seguros")', timeout=8_000)
        time.sleep(4)
    except Exception:
        log.warn("Sidebar 'seguros' não encontrado — usando URL directa")
        try:
            page.goto(APOLICES_URL, timeout=20_000)
            time.sleep(4)
        except Exception as e:
            log.error(f"Falha ao navegar para apólices: {e}")
            return []

    if debug:
        page.screenshot(path="/tmp/fidelidade_apolices.png")

    detalhes = page.query_selector_all('a:has-text("DETALHE DO SEGURO")')
    n_found  = len(detalhes)
    log.debug(f"Lista de apólices carregada — {n_found} DETALHE link(s)")

    if n_found == 0:
        log.warn("Nenhum link 'DETALHE DO SEGURO' encontrado na página")
        return []

    policies = []

    for i in range(n_found):
        detalhes = page.query_selector_all('a:has-text("DETALHE DO SEGURO")')
        if i >= len(detalhes):
            log.warn(f"Apólice {i+1}: link desapareceu — DOM mudou inesperadamente")
            break

        try:
            detalhes[i].click()
            time.sleep(3)
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception as e:
            log.warn(f"Apólice {i+1}: falha ao abrir detalhe — {e}")
            try:
                page.go_back()
                time.sleep(2)
            except Exception:
                pass
            continue

        detail_text = page.inner_text("body")

        # Policy number
        apolice_num = ""
        m = re.search(r"Apólice\s+n[oº°]\s+([A-Z0-9]+)", detail_text, re.IGNORECASE)
        if m:
            apolice_num = m.group(1)

        # Product name
        product_name = ""
        lines = detail_text.split("\n")
        for idx, ln in enumerate(lines):
            if "OS MEUS SEGUROS" in ln:
                for ln2 in lines[idx + 1: idx + 6]:
                    ln2 = ln2.strip()
                    if ln2 and ln2 not in ("OS MEUS SEGUROS", "TODOS OS SEGUROS"):
                        product_name = ln2
                        break
                break

        if not apolice_num:
            log.warn(f"Apólice {i+1} ({product_name or '?'}): número não encontrado — ignorada")
            try:
                page.go_back()
                time.sleep(2)
            except Exception:
                pass
            continue

        policy = _parse_detail_page(detail_text, product_name, apolice_num)
        policies.append(policy)
        log.debug(f"Apólice {i+1} extraída: {apolice_num} ({policy['category']}) "
                  f"end={policy.get('end_date')} premium={policy.get('premium_amount')}")

        try:
            page.go_back()
            time.sleep(2)
        except Exception as e:
            log.warn(f"Apólice {i+1}: falha ao voltar atrás — {e}")

    return policies


# ── DB upsert ──────────────────────────────────────────────────────────────────

def upsert_policy(conn, policy: dict, log: RunLog, dry_run: bool) -> tuple[str, int | None]:
    """
    INSERT se não existir, UPDATE se já existir.
    Duplicados detectados por policy_number (exact match).
    """
    pn = (policy.get("policy_number") or "").strip()
    if not pn:
        log.warn(f"Apólice sem policy_number — ignorada: {policy.get('coverage_summary', '?')}")
        return "skipped", None

    # Check for existing record
    row = conn.execute(
        sa.text("SELECT id FROM public.insurance_policies WHERE policy_number = :pn"),
        {"pn": pn},
    ).mappings().first()

    cat      = policy.get("category") or ""
    start    = policy.get("start_date")
    end      = policy.get("end_date")
    renewal  = policy.get("renewal_date")
    premium  = policy.get("premium_amount") or None
    freq     = policy.get("payment_frequency") or "Anual"
    coverage = policy.get("coverage_summary") or ""
    notes    = policy.get("notes") or "Importado de MyFidelidade"

    if dry_run:
        if row:
            log.info(f"[dry-run] ACTUALIZAR {pn} ({cat}) — end={end} premium={premium}€")
        else:
            log.info(f"[dry-run] CRIAR {pn} ({cat}) — end={end} premium={premium}€")
        return ("would-update" if row else "would-create"), (row["id"] if row else None)

    mat    = policy.get("vehicle_matricula")
    marca  = policy.get("vehicle_marca")
    modelo = policy.get("vehicle_modelo")

    now = datetime.utcnow()
    if row:
        conn.execute(
            sa.text("""
                UPDATE public.insurance_policies
                SET category=COALESCE(NULLIF(:cat,''), category),
                    start_date=COALESCE(:start, start_date),
                    end_date=COALESCE(:end, end_date),
                    renewal_date=COALESCE(:renewal, renewal_date),
                    premium_amount=COALESCE(:premium, premium_amount),
                    payment_frequency=COALESCE(NULLIF(:freq,''), payment_frequency),
                    coverage_summary=COALESCE(NULLIF(:coverage,''), coverage_summary),
                    notes=COALESCE(NULLIF(:notes,''), notes),
                    updated_at=:now,
                    vehicle_matricula=COALESCE(:mat, vehicle_matricula),
                    vehicle_marca=COALESCE(:marca, vehicle_marca),
                    vehicle_modelo=COALESCE(:modelo, vehicle_modelo)
                WHERE policy_number=:pn
            """),
            dict(cat=cat, start=start, end=end, renewal=renewal,
                 premium=premium, freq=freq, coverage=coverage,
                 notes=notes, now=now, pn=pn,
                 mat=mat, marca=marca, modelo=modelo),
        )
        log.info(f"Actualizada: {pn} ({cat}) — end={end}" + (f" mat={mat}" if mat else ""))
        return "updated", row["id"]
    else:
        r = conn.execute(
            sa.text("""
                INSERT INTO public.insurance_policies
                  (entity_type, entity_ref, insurer_name, policy_number, category,
                   coverage_summary, start_date, end_date, renewal_date,
                   premium_amount, payment_frequency, auto_renew, status, notes,
                   vehicle_matricula, vehicle_marca, vehicle_modelo,
                   created_at, updated_at)
                VALUES
                  (:entity_type, :entity_ref, :insurer_name, :pn, :cat,
                   :coverage, :start, :end, :renewal,
                   :premium, :freq, true, 'active', :notes,
                   :mat, :marca, :modelo,
                   :now, :now)
                RETURNING id
            """),
            dict(entity_type=policy.get("entity_type", "particular"),
                 entity_ref=policy.get("entity_ref", f"NIF {NIF}"),
                 insurer_name="Fidelidade",
                 pn=pn, cat=cat, coverage=coverage,
                 start=start, end=end, renewal=renewal,
                 premium=premium, freq=freq, notes=notes,
                 mat=mat, marca=marca, modelo=modelo, now=now),
        )
        new_id = r.mappings().first()["id"]
        log.info(f"Criada: {pn} ({cat}) — end={end} id={new_id}" + (f" mat={mat}" if mat else ""))
        return "created", new_id


# ── Main ───────────────────────────────────────────────────────────────────────

def run(dry_run=False, safe=False, debug=False,
        nif=None, password=None, personal_nif=None,
        entity_type=None, entity_ref=None,
        empresa=False) -> dict:
    from playwright.sync_api import sync_playwright

    _nif  = nif or NIF
    _pass = password or PASS
    _pnif = personal_nif or PERSONAL_NIF

    log = RunLog(dry_run=dry_run)

    mode_tag = " [DRY-RUN]" if dry_run else ""
    safe_tag = " [SAFE]" if safe else ""
    emp_tag  = " [EMPRESA]" if empresa else ""
    print(f"\nfidelidade_scraper{mode_tag}{safe_tag}{emp_tag}")
    print(f"  Início  : {log.started.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  NIF     : {_nif[:3]}{'*' * 6}")
    print(f"  Portal  : {'empresasmy.fidelidade.pt' if empresa else 'my.fidelidade.pt'}")
    print(f"  Log     : {LOG_FILE}")
    print()

    result = {"ok": False, "created": 0, "updated": 0, "skipped": 0, "errors": []}

    # ── Browser / login ──
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="pt-PT",
            )
            page = ctx.new_page()

            print("Autenticação")
            if empresa:
                ok = empresa_login(page, log, debug=debug, nif=_nif, password=_pass)
                login_err = "Login empresa falhou — verifique FIDELIDADE_NIF / FIDELIDADE_PASS em /etc/aios.env"
            else:
                ok = login(page, log, debug=debug, nif=_nif, password=_pass, personal_nif=_pnif)
                login_err = "Login falhou — verifique FIDELIDADE_NIF / FIDELIDADE_PASS em /etc/aios.env"

            if not ok:
                browser.close()
                log.error(login_err)
                result["errors"].append(login_err)
                if safe:
                    print(f"\n  --safe activo: a parar sem scraping parcial\n")
                log.flush(result)
                return result

            print(f"  ✓ Autenticado → {page.url[:70]}")
            print()
            print("Extracção de apólices")

            if empresa:
                policies = scrape_empresa_policies(page, log, debug=debug)
            else:
                policies = scrape_policies(page, log, debug=debug)
            browser.close()

            # Override entity fields if explicitly provided
            if entity_type or entity_ref:
                for pol in policies:
                    if entity_type:
                        pol["entity_type"] = entity_type
                    if entity_ref:
                        pol["entity_ref"] = entity_ref

    except Exception as e:
        err = f"Erro inesperado durante scraping: {e}"
        log.error(err)
        result["errors"].append(err)
        if debug:
            traceback.print_exc()
        log.flush(result)
        return result

    n_scraped = len(policies)
    print(f"  ✓ {n_scraped} apólice(s) extraída(s) do portal")

    if n_scraped == 0:
        log.warn("Nenhuma apólice extraída — verifique o portal manualmente")
        result["ok"] = True
        log.flush(result)
        return result

    # ── DB ──
    print()
    print("Base de dados")
    try:
        engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
        with engine.begin() as conn:
            for pol in policies:
                try:
                    action, pid = upsert_policy(conn, pol, log, dry_run=dry_run)
                    if "create" in action:
                        result["created"] += 1
                    elif "update" in action:
                        result["updated"] += 1
                    else:
                        result["skipped"] += 1
                except Exception as e:
                    pn = pol.get("policy_number", "?")
                    err = f"Erro ao gravar apólice {pn}: {e}"
                    log.error(err)
                    result["errors"].append(err)
                    result["skipped"] += 1

    except Exception as e:
        err = f"Erro de ligação à BD: {e}"
        log.error(err)
        result["errors"].append(err)
        log.flush(result)
        return result

    result["ok"] = True

    # ── Summary ──
    ended    = datetime.utcnow()
    duration = round((ended - log.started).total_seconds(), 1)

    print()
    print("─" * 50)
    if dry_run:
        print(f"  [DRY-RUN] Sem alterações na BD")
    else:
        print(f"  Criadas    : {result['created']}")
        print(f"  Actualizadas: {result['updated']}")
        print(f"  Ignoradas  : {result['skipped']}")
    if result["errors"]:
        print(f"  Erros      : {len(result['errors'])}")
        for e in result["errors"]:
            print(f"    - {e}")
    print(f"  Duração    : {duration}s")
    print(f"  Fim        : {ended.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print()

    log.flush(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MyFidelidade scraper")
    parser.add_argument("--dry-run",      action="store_true", help="Não escreve na BD")
    parser.add_argument("--safe",         action="store_true", help="Para se login falhar")
    parser.add_argument("--debug",        action="store_true", help="Screenshots + logs detalhados")
    parser.add_argument("--empresa",      action="store_true", help="Usar portal empresasmy.fidelidade.pt")
    parser.add_argument("--nif",          help="NIF de acesso (override de FIDELIDADE_NIF)")
    parser.add_argument("--pass",         dest="password", help="Password (override de FIDELIDADE_PASS)")
    parser.add_argument("--personal-nif", dest="personal_nif", help="NIF pessoal para validação 2FA")
    parser.add_argument("--entity-type",  dest="entity_type",
                        help="Forçar tipo de entidade: particular|company|vehicle")
    parser.add_argument("--entity-ref",   dest="entity_ref", help="Forçar ref. entidade")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run, safe=args.safe, debug=args.debug,
                 nif=args.nif, password=args.password, personal_nif=args.personal_nif,
                 entity_type=args.entity_type, entity_ref=args.entity_ref,
                 empresa=args.empresa)
    # Machine-readable summary on last line
    print(json.dumps({k: v for k, v in result.items() if k != "errors"}, default=str))
    sys.exit(0 if result["ok"] else 1)
