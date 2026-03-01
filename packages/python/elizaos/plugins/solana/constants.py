"""Solana program IDs, PDA seeds, and API endpoints."""

PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_FUN_FEE_RECIPIENT = "CebN5WGQ4jvEPvsVU4EoHEpgznyQHeGKuibeABMPiwVS"
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1brs"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
RENT_SYSVAR = "SysvarRent111111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

JUPITER_PRICE_API = "https://lite.jupiterapi.com/price"
JUPITER_QUOTE_API = "https://lite.jupiterapi.com/quote"
JUPITER_SWAP_API = "https://lite.jupiterapi.com/swap"

# Anchor discriminators (sha256("global:<fn>")[0:8])
PUMP_FUN_BUY_DISCRIMINATOR = b"\xf2\x23\xc6\x89\x52\xe1\x45\xad"
PUMP_FUN_SELL_DISCRIMINATOR = b"\x33\xe6\x85\xa4\x01\x7f\x83\xad"

# Pump.fun bonding curve account layout (offset 8 to skip discriminator)
# virtual_token_reserves(u64) virtual_sol_reserves(u64) real_token_reserves(u64)
# real_sol_reserves(u64) token_total_supply(u64) complete(bool)
BONDING_CURVE_LAYOUT = "<QQQQQb"
BONDING_CURVE_LAYOUT_OFFSET = 8
