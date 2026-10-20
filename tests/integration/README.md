# Pruebas de integración real (opt-in)

Estos módulos crean su propio PostgreSQL descartable, lanzan el Merchant congelado en procesos
separados y ejercitan el Agent real. No corren en la suite normal ni en CI. Cada módulo exige
dos condiciones: su activación explícita y las ubicaciones de la máquina. Si falta alguna,
pytest omite el módulo y explica el motivo:

```sh
uv run pytest tests/integration -rs
```

| Módulo | Activación | Merchant congelado | Agent | Sistemas |
| --- | --- | --- | --- | --- |
| `test_preselection_merchant.py` (07B-P) | `RUN_GOV07BP_REAL=1` | `9743c9f` | checkout actual | Windows, macOS, Linux |
| `test_guarded_d1_live.py` (07B-B) | `RUN_GOV07BB_REAL=1` | `9743c9f` | checkout actual | Windows, macOS, Linux |
| `test_live_creation_recovery.py` (06D-C) | `RUN_GOV06DC_REAL=1` | `c5bd811` | exactamente `4b890cb` | solo Windows |
| `test_initial_dispatch_live.py` (07A) | `RUN_GOV07A_REAL=1` | `c5bd811` | exactamente `9f15d20` | solo Windows |

Los SHA completos están en los módulos (`MERCHANT_HEAD`, `AGENT_HEAD`, `HEAD` y la aserción de
`MerchantFixture`). Las pruebas verifican esos commits y un árbol limpio antes de iniciar
procesos.

## Variables de ubicación

No tienen valores por defecto. Solo las lee el proceso de pytest; los procesos hijos reciben
rutas explícitas y un entorno limpio (`clean_environment`).

- `MERCHANT_REPO`: checkout limpio del Merchant en el commit congelado de la tabla, con su
  entorno instalado (`uv sync --frozen`). Es el mismo nombre que usa el E2E conjunto. El
  Merchant corre con su propio intérprete: `.venv/bin/python`, o `.venv\Scripts\python.exe`
  en Windows. El Agent auxiliar de 07B-B usa el intérprete de este checkout (`sys.executable`).
- `PG_BIN_DIR`: directorio `bin` de PostgreSQL 17 con `initdb`, `pg_ctl`, `psql` y `postgres`.
  El sufijo `.exe` se agrega solo en Windows.

El arnés inicializa, arranca y detiene su propio clúster bajo `.local/` del Agent (ignorado por
Git), con puertos de loopback libres. No reutiliza un servicio PostgreSQL del sistema y no
necesita que haya uno en marcha.

## macOS y Linux (07B-P y 07B-B)

1. Instale los binarios de PostgreSQL 17:
   - macOS con Homebrew: `brew install postgresql@17`; luego
     `PG_BIN_DIR="$(brew --prefix postgresql@17)/bin"`.
   - Debian o Ubuntu con el repositorio PGDG: `sudo apt install postgresql-17`; luego
     `PG_BIN_DIR=/usr/lib/postgresql/17/bin`.
2. Prepare el Merchant congelado en un worktree aparte, para que el checkout principal siga
   libre:

   ```sh
   git -C ../tesis-commerce-merchant worktree add ../merchant-07b \
     9743c9f179c7ad5413e8818766ef3dceb326a917
   (cd ../merchant-07b && uv sync --frozen)
   ```

3. Ejecute cada módulo con su activación:

   ```sh
   export MERCHANT_REPO="$PWD/../merchant-07b"
   export PG_BIN_DIR="$(brew --prefix postgresql@17)/bin"  # Linux: /usr/lib/postgresql/17/bin
   RUN_GOV07BP_REAL=1 uv run pytest tests/integration/test_preselection_merchant.py -rs
   RUN_GOV07BB_REAL=1 uv run pytest tests/integration/test_guarded_d1_live.py -rs
   ```

Estas rutas multiplataforma se comprobaron en recolección y omisión. Todavía no hay una
ejecución real en macOS ni en Linux. Una primera corrida debe tratarse como validación nueva,
con su evidencia, y no como repetición de la evidencia de Windows.

## Windows

```powershell
$env:MERCHANT_REPO = 'C:\ruta\a\merchant-07b'
$env:PG_BIN_DIR = 'C:\Program Files\PostgreSQL\17\bin'
$env:RUN_GOV07BP_REAL = '1'
uv run pytest tests/integration/test_preselection_merchant.py -rs
Remove-Item Env:RUN_GOV07BP_REAL
```

## Por qué 06D-C y 07A siguen siendo solo Windows

- El arnés `gov06dc_harness.py` mantiene bajo su control el listener HTTP del Merchant y lo
  entrega al proceso hijo con `socket.share` y `socket.fromshare`, que solo existen en Windows.
  En macOS y Linux la omisión lo indica. No se implementó un equivalente POSIX, como heredar el
  descriptor con `pass_fds`, porque cambiaría el arnés auditado y no puede validarse sin
  ejecutar los casos reales.
- Además, ambos módulos exigen que el Agent esté exactamente en su commit auditado, con solo
  los cambios permitidos. Desde `main` esa verificación falla a propósito: los módulos
  reproducen la evidencia histórica, no la versión actual del Agent.
