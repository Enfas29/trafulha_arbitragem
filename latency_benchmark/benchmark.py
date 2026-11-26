"""One-shot read-only benchmark; no database, scanner or orders."""
import math, os, statistics, time
from concurrent.futures import ThreadPoolExecutor, wait
import requests

def measure(session, url, params, required):
    start = time.perf_counter()
    r = session.get(url, params=params, timeout=7, allow_redirects=False)
    if r.status_code != 200:
        raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
    data = r.json()
    if not isinstance(data, dict) or required not in data or data.get("status") == "failure":
        raise ValueError("Invalid response")
    return (time.perf_counter() - start) * 1000

def report(name, values):
    if not values:
        print(f"{name}: sem amostras validas", flush=True)
        return
    values = sorted(values)
    p95 = values[math.ceil(.95 * len(values)) - 1]
    print(f"{name}: n={len(values)} | mediana={statistics.median(values):.1f} ms | p95={p95:.1f} ms | media={statistics.mean(values):.1f} ms", flush=True)

def main():
    key = os.getenv("SX_API_KEY", "").strip()
    if not key:
        raise SystemExit("SX_API_KEY ausente.")
    asset = os.environ["BENCH_POLY_ASSET"]
    market = os.environ["BENCH_SX_MARKET"]
    print("REGIAO:", os.getenv("RAILWAY_REPLICA_REGION", "desconhecida"), flush=True)
    print("POLY ASSET:", asset, "SX MARKET:", market, flush=True)
    samples = [[], [], []]
    failures = [0, 0]
    warmup_errors = attempted = 0
    with requests.Session() as poly, requests.Session() as sx, ThreadPoolExecutor(max_workers=2) as pool:
        sx.headers["x-sx-api-key"] = key
        for i in range(103):
            start = time.perf_counter()
            jobs = [pool.submit(measure, poly, "https://clob.polymarket.com/book", {"token_id": asset}, "asks"),
                    pool.submit(measure, sx, "https://api.sx.bet/orderbook-v3/snapshot/market", {"marketHash": market}, "data")]
            wait(jobs)
            elapsed = (time.perf_counter() - start) * 1000
            values = [None, None]
            stop = False
            if i >= 3:
                attempted += 1
            for j, job in enumerate(jobs):
                try:
                    values[j] = job.result()
                    if i >= 3:
                        samples[j].append(values[j])
                except Exception as exc:
                    status = exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response is not None else None
                    print(f"ERRO {i+1} {('POLY','SX')[j]}: {type(exc).__name__} HTTP={status}", flush=True)
                    if i >= 3:
                        failures[j] += 1
                    else:
                        warmup_errors += 1
                    if status in (401, 403, 404, 429):
                        stop = True
            if i >= 3 and all(v is not None for v in values):
                samples[2].append(elapsed)
            if stop:
                print("Interrompido por acesso, limite ou mercado indisponivel. Sem retries.", flush=True)
                break
            if (i + 1) % 20 == 0:
                print(f"Progresso: {i+1}/103", flush=True)
            if i < 102:
                time.sleep(1)
    for name, values in zip(("Polymarket", "SX Bet", "AMBAS em paralelo"), samples):
        report(name, values)
    print(f"Tentativas medidas: {attempted}/100 | erros POLY={failures[0]} | erros SX={failures[1]} | pares invalidos={attempted-len(samples[2])} | erros aquecimento={warmup_errors}", flush=True)
    print("BENCHMARK_TERMINADO", flush=True)

if __name__ == "__main__":
    main()
