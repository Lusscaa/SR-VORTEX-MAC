"""Converte o .txt do fornecedor para o formato combolist do Vortex."""
import re
import sys
from pathlib import Path

def convert(input_path: str) -> None:
    src = Path(input_path)
    if not src.exists():
        print(f"Arquivo não encontrado: {src}")
        sys.exit(1)

    lines = src.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    accounts = []

    for line in lines:
        # Linha com dados: "Username password 2fa mail password: <TAB-separated fields>"
        m = re.match(r"^\s*Username password.*?:\s*(.+)$", line)
        if not m:
            continue
        parts = [p.strip() for p in m.group(1).split("\t") if p.strip()]
        if len(parts) < 3:
            continue
        # Ordem do fornecedor: login | senha | 2FA | email | senha_email
        accounts.append("|".join(parts))

    if not accounts:
        print("Nenhuma conta encontrada no arquivo.")
        sys.exit(1)

    out = src.parent / "contas_vortex.txt"
    out.write_text("\n".join(accounts) + "\n", encoding="utf-8")
    print(f"{len(accounts)} conta(s) convertida(s) -> {out}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python converter_contas.py <arquivo.txt>")
        sys.exit(1)
    convert(sys.argv[1])
