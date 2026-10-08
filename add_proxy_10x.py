#!/usr/bin/env python3
"""
Script CLI pra adicionar 1 proxy duplicada 10x no estoque.

Uso:
    python add_proxy_10x.py "185.14.238.9:36421:user:pass"
    python add_proxy_10x.py "185.14.238.9:36421:user:pass" 20  # 20 vezes
"""
import sys
import requests

def main():
    if len(sys.argv) < 2:
        print("❌ Uso: python add_proxy_10x.py <proxy> [quantidade]")
        print("Exemplo: python add_proxy_10x.py '185.14.238.9:36421:user:pass'")
        print("Exemplo: python add_proxy_10x.py '185.14.238.9:36421:user:pass' 20")
        sys.exit(1)

    proxy = sys.argv[1]
    times = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    try:
        resp = requests.post(
            "http://localhost:8000/api/pools/proxies/duplicate",
            json={"proxy": proxy, "times": times},
            headers={"Authorization": "Bearer testtoken"}
        )
        data = resp.json()

        if resp.status_code == 200:
            print(f"✅ Adicionadas {data['added']}/{times} proxies")
        else:
            print(f"❌ Erro: {data.get('error', 'desconhecido')}")
            sys.exit(1)

    except requests.exceptions.ConnectionError:
        print("❌ Erro: não consegui conectar ao backend (está rodando na porta 8000?)")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Erro: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
