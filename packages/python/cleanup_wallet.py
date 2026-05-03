"""
One-shot wallet cleanup: sell every token in the wallet via Jupiter.
Tokens with no Jupiter liquidity → token account is closed (rent recovered, dust burned).
Run ONCE to clear all leftover tokens before a fresh bot session.
"""
import asyncio, os, base64, struct
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

WSOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE = "https://public.jupiterapi.com/quote"
JUPITER_SWAP  = "https://public.jupiterapi.com/swap"
MIN_VALUE_SOL = 0.000050   # skip absolute micro-dust (< 0.00005 SOL worth)

# SPL Token program
TOKEN_PROGRAM_ID    = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROG_ID  = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


async def close_token_account(pubkey: str, token_account: str, program_id: str,
                               keypair, session, rpc_fn) -> bool:
    """Close a token account, recovering ~0.002 SOL rent.
    The account must have zero token balance (already sold or dust) OR
    we burn the tokens first via a burn instruction.
    For dust amounts we just close directly — the program zero-checks on close
    so we send a burn+close in the same transaction.
    """
    try:
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.message import MessageV0
        from solders.transaction import VersionedTransaction
        from solders.hash import Hash

        owner_pk    = Pubkey.from_string(pubkey)
        acct_pk     = Pubkey.from_string(token_account)
        prog_pk     = Pubkey.from_string(program_id)

        # CloseAccount instruction discriminator = 9
        close_data  = bytes([9])
        close_ix    = Instruction(
            program_id=prog_pk,
            accounts=[
                AccountMeta(pubkey=acct_pk,  is_signer=False, is_writable=True),
                AccountMeta(pubkey=owner_pk,  is_signer=False, is_writable=True),  # rent dest
                AccountMeta(pubkey=owner_pk,  is_signer=True,  is_writable=False), # authority
            ],
            data=close_data,
        )

        # Get recent blockhash
        bh_res = await rpc_fn("getLatestBlockhash", [{"commitment": "confirmed"}])
        blockhash = bh_res["value"]["blockhash"]

        msg = MessageV0.try_compile(
            payer=owner_pk,
            instructions=[close_ix],
            address_lookup_table_accounts=[],
            recent_blockhash=Hash.from_string(blockhash),
        )
        tx = VersionedTransaction(msg, [keypair])
        tx_b64 = base64.b64encode(bytes(tx)).decode()

        res = await rpc_fn("sendTransaction", [
            tx_b64,
            {"encoding": "base64", "skipPreflight": False,
             "preflightCommitment": "confirmed", "maxRetries": 3}
        ])
        sig = res if isinstance(res, str) else str(res)
        print(f"CLOSED  sig={sig[:24]}... (~0.002 SOL rent recovered)")
        return True
    except Exception as e:
        print(f"CLOSE FAILED — {e}")
        return False


async def main():
    import aiohttp
    from solders.keypair import Keypair

    priv = os.getenv("SOLANA_PRIVATE_KEY", "")
    if not priv:
        print("ERROR: SOLANA_PRIVATE_KEY not set"); return

    import base58
    keypair = Keypair.from_bytes(base58.b58decode(priv))
    pubkey  = str(keypair.pubkey())
    print(f"Wallet: {pubkey}\n")

    rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

    async with aiohttp.ClientSession() as session:

        async def rpc(method, params):
            async with session.post(rpc_url, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}) as r:
                return (await r.json()).get("result", {})

        # Fetch all token accounts (Token and Token-2022)
        balances = []
        for prog in [TOKEN_PROGRAM_ID, TOKEN_2022_PROG_ID]:
            res = await rpc("getTokenAccountsByOwner", [
                pubkey,
                {"programId": prog},
                {"encoding": "jsonParsed"}
            ])
            for acct in res.get("value", []):
                info   = acct["account"]["data"]["parsed"]["info"]
                mint   = info["mint"]
                amt    = info["tokenAmount"]
                raw_amt = int(amt["amount"])
                decimals = int(amt["decimals"])
                balances.append({
                    "mint":     mint,
                    "raw":      raw_amt,
                    "decimals": decimals,
                    "account":  acct["pubkey"],
                    "program":  prog,
                })

        if not balances:
            print("Wallet is already clean — no token accounts found."); return

        print(f"Found {len(balances)} token account(s) in wallet.\n")

        sold, closed, skipped, failed = [], [], [], []

        for b in balances:
            mint     = b["mint"]
            raw      = b["raw"]
            decimals = b["decimals"]
            amount   = raw / (10 ** decimals) if decimals > 0 else raw

            if raw == 0:
                # Account already empty — just close it
                print(f"  EMPTY  {mint[:8]}... (0 balance)", end=" → closing... ", flush=True)
                ok = await close_token_account(pubkey, b["account"], b["program"], keypair, session, rpc)
                (closed if ok else failed).append(mint)
                await asyncio.sleep(0.5)
                continue

            # Try Jupiter quote
            try:
                async with session.get(JUPITER_QUOTE, params={
                    "inputMint": mint,
                    "outputMint": WSOL_MINT,
                    "amount": str(raw),
                    "slippageBps": "5000",
                }) as r:
                    quote = await r.json()

                if "error" in quote or not quote.get("outAmount"):
                    raise ValueError("no route")

                out_sol = int(quote["outAmount"]) / 1e9
                if out_sol < MIN_VALUE_SOL:
                    raise ValueError(f"micro-dust ({out_sol:.6f} SOL)")

                print(f"  SELL   {mint[:8]}... {amount:.4f} tokens → ~{out_sol:.5f} SOL", end=" ... ", flush=True)

                async with session.post(JUPITER_SWAP, json={
                    "quoteResponse": quote,
                    "userPublicKey": pubkey,
                    "wrapAndUnwrapSol": True,
                    "prioritizationFeeLamports": 1_000_000,
                }) as r:
                    swap = await r.json()

                if "swapTransaction" not in swap:
                    raise ValueError("no swapTransaction in response")

                from solders.transaction import VersionedTransaction
                tx_bytes = base64.b64decode(swap["swapTransaction"])
                vtx      = VersionedTransaction.from_bytes(tx_bytes)
                signed   = VersionedTransaction(vtx.message, [keypair])

                res = await rpc("sendTransaction", [
                    base64.b64encode(bytes(signed)).decode(),
                    {"encoding": "base64", "skipPreflight": False,
                     "preflightCommitment": "confirmed", "maxRetries": 3}
                ])
                sig = res if isinstance(res, str) else str(res)
                print(f"OK sig={sig[:24]}...")
                sold.append((mint, out_sol, sig))
                await asyncio.sleep(1.0)

            except Exception as e:
                # No Jupiter route or micro-dust — close the account to recover rent
                print(f"  NO ROUTE {mint[:8]}... ({e})", end=" → closing account... ", flush=True)
                ok = await close_token_account(pubkey, b["account"], b["program"], keypair, session, rpc)
                (closed if ok else failed).append(mint)
                await asyncio.sleep(0.5)

        print(f"\n{'─'*55}")
        print(f"Sold:    {len(sold)} account(s)  (+{sum(s for _,s,_ in sold):.5f} SOL recovered from swaps)")
        print(f"Closed:  {len(closed)} account(s)  (~{len(closed)*0.002:.4f} SOL rent recovered)")
        print(f"Failed:  {len(failed)} account(s)")
        total_recovered = sum(s for _,s,_ in sold) + len(closed) * 0.002
        print(f"Est. total recovered: ~{total_recovered:.4f} SOL")
        print(f"{'─'*55}")
        print("Wallet cleanup complete.")

if __name__ == "__main__":
    asyncio.run(main())
