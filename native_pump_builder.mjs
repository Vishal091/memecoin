import {
  ComputeBudgetProgram,
  Connection,
  PublicKey,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
import BN from "bn.js";
import {
  OnlinePumpSdk,
  PUMP_SDK,
  getBuyV3TokenAmountFromQuoteAmount,
  getSellSolAmountFromTokenAmount,
} from "@pump-fun/pump-sdk";
import {
  OnlinePumpAmmSdk,
  PUMP_AMM_SDK,
  canonicalPumpPoolPda,
} from "@pump-fun/pump-swap-sdk";

const PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
const WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112";
const ZERO_PUBKEY = "11111111111111111111111111111111";
const COMPUTE_UNITS = 400_000;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function readInput() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  return JSON.parse(input);
}

function sanitizeError(error, rpcUrl) {
  let message = String(error?.stack || error?.message || error || "Unknown native builder error");
  if (rpcUrl) message = message.split(rpcUrl).join("[configured RPC URL]");
  return message.slice(0, 1800);
}

async function checkRuntime() {
  assert(typeof OnlinePumpSdk === "function", "Missing OnlinePumpSdk from @pump-fun/pump-sdk");
  assert(typeof PUMP_SDK?.buyExactQuoteInV3Instructions === "function", "Missing official Pump v3 buy builder");
  assert(typeof PUMP_SDK?.sellV3Instructions === "function", "Missing official Pump v3 sell builder");
  assert(typeof getBuyV3TokenAmountFromQuoteAmount === "function", "Missing Pump v3 quote helper");
  assert(typeof getSellSolAmountFromTokenAmount === "function", "Missing Pump sell quote helper");
  assert(typeof OnlinePumpAmmSdk === "function", "Missing OnlinePumpAmmSdk from @pump-fun/pump-swap-sdk");
  assert(typeof canonicalPumpPoolPda === "function", "Missing canonicalPumpPoolPda from PumpSwap SDK");
  assert(typeof PUMP_AMM_SDK?.buyQuoteInput === "function", "Missing official PumpSwap buy builder");
  assert(typeof PUMP_AMM_SDK?.sellBaseInput === "function", "Missing official PumpSwap sell builder");
  process.stdout.write(JSON.stringify({ ok: true, builder: "official-pump-sdks", router: "not-used" }) + "\n");
}

async function build(input) {
  const rpcUrl = String(input.rpcUrl || "");
  assert(/^https:\/\//i.test(rpcUrl), "rpcUrl must use HTTPS");
  const wallet = new PublicKey(input.wallet);
  const mint = new PublicKey(input.mint);
  const action = String(input.action || "");
  assert(action === "buy" || action === "sell", "action must be buy or sell");
  const slippageBps = Number(input.slippageBps);
  assert(Number.isInteger(slippageBps) && slippageBps >= 0 && slippageBps <= 500, "slippageBps out of range");

  const amountLamports = BigInt(String(input.amountLamports || "0"));
  const amountRaw = BigInt(String(input.amountRaw || "0"));
  assert(action === "sell" ? amountRaw > 0n : amountLamports > 0n, "trade amount must be positive");
  const priorityFeeSol = Number(input.priorityFeeSol || 0);
  assert(Number.isFinite(priorityFeeSol) && priorityFeeSol >= 0, "priorityFeeSol must be non-negative");

  const connection = new Connection(rpcUrl, {
    commitment: "confirmed",
    confirmTransactionInitialTimeout: 20_000,
  });
  const onlinePump = new OnlinePumpSdk(connection);
  const state = action === "buy"
    ? await onlinePump.fetchBuyState(mint, wallet)
    : await onlinePump.fetchSellState(mint, wallet);
  const curve = state.bondingCurve;
  const quoteMint = state.quoteMint.toBase58();
  assert(
    quoteMint === WRAPPED_SOL_MINT || quoteMint === ZERO_PUBKEY,
    "Native executor only supports SOL-quoted Pump tokens; refusing a token-quoted coin.",
  );
  const slippagePercent = slippageBps / 100;
  let instructions;
  let route;
  let expectedProgram;

  if (!curve.complete) {
    // V3 bonding-curve instructions are published by Pump.fun and do not use
    // PumpPortal's outer router. Cashback coins are deliberately refused here:
    // their documented route requires the separate v2 builder.
    assert(!curve.isCashbackCoin, "Cashback coin is not enabled in the native v3 execution path.");
    const [global, feeConfig, supplyInfo] = await Promise.all([
      onlinePump.fetchGlobal(),
      onlinePump.fetchFeeConfig(),
      connection.getTokenSupply(mint, "confirmed"),
    ]);
    const mintSupply = new BN(supplyInfo.value.amount);
    assert(mintSupply.gt(new BN(0)), "Mint supply is zero; refusing to build a curve trade.");

    if (action === "buy") {
      const quoteAmount = new BN(amountLamports.toString());
      const tokensOut = getBuyV3TokenAmountFromQuoteAmount({
        global,
        feeConfig,
        mintSupply,
        bondingCurve: curve,
        amount: quoteAmount,
        curveBaseTokenBalance: state.curveBaseTokenBalance,
      });
      assert(tokensOut.gt(new BN(0)), "Pump v3 quote returned zero tokens.");
      instructions = await PUMP_SDK.buyExactQuoteInV3Instructions({
        bondingCurve: curve,
        associatedUserAccountInfo: state.associatedUserAccountInfo,
        mint,
        user: wallet,
        amount: tokensOut,
        quoteAmount,
        slippage: slippagePercent,
        tokenProgram: state.tokenProgram,
        quoteTokenProgram: state.quoteTokenProgram,
      });
    } else {
      const tokenAmount = new BN(amountRaw.toString());
      const expectedSol = getSellSolAmountFromTokenAmount({
        global,
        feeConfig,
        mintSupply,
        bondingCurve: curve,
        amount: tokenAmount,
      });
      assert(expectedSol.gt(new BN(0)), "Pump sell quote returned zero SOL.");
      instructions = await PUMP_SDK.sellV3Instructions({
        bondingCurve: curve,
        mint,
        user: wallet,
        amount: tokenAmount,
        quoteAmount: expectedSol,
        slippage: slippagePercent,
        tokenProgram: state.tokenProgram,
        quoteTokenProgram: state.quoteTokenProgram,
      });
    }
    route = "pump";
    expectedProgram = PUMP_PROGRAM_ID;
  } else {
    // Graduated coins use the canonical PumpSwap pool. The SDK builds the
    // official PumpSwap instruction directly; no PumpPortal router is involved.
    const poolKey = canonicalPumpPoolPda(mint);
    const onlineAmm = new OnlinePumpAmmSdk(connection);
    const swapState = await onlineAmm.swapSolanaState(poolKey, wallet);
    assert(swapState.pool.baseMint.equals(mint), "Canonical PumpSwap pool base mint does not match the requested mint.");
    assert(
      swapState.pool.quoteMint.toBase58() === WRAPPED_SOL_MINT,
      "Native executor only supports WSOL-quoted canonical PumpSwap pools.",
    );

    if (action === "buy") {
      // PumpSwap's quote-input helper raises maxQuote by slippage. Lower the
      // quote target first so the instruction's worst-case spend remains within
      // the user's configured entry amount.
      const safeQuoteInput = amountLamports * 10_000n / BigInt(10_000 + slippageBps);
      assert(safeQuoteInput > 0n, "Trade size is too small after slippage allowance.");
      instructions = await PUMP_AMM_SDK.buyQuoteInput(
        swapState,
        new BN(safeQuoteInput.toString()),
        slippagePercent,
        { v2: true },
      );
    } else {
      instructions = await PUMP_AMM_SDK.sellBaseInput(
        swapState,
        new BN(amountRaw.toString()),
        slippagePercent,
        { v2: true },
      );
    }
    route = "pump-amm";
    expectedProgram = PUMPSWAP_PROGRAM_ID;
  }

  assert(Array.isArray(instructions) && instructions.length > 0, "Official SDK returned no instructions.");
  const priorityFeeLamports = Math.floor(priorityFeeSol * 1_000_000_000);
  const microLamportsPerCu = priorityFeeLamports > 0
    ? Math.ceil(priorityFeeLamports * 1_000_000 / COMPUTE_UNITS)
    : 0;
  const allInstructions = [
    ComputeBudgetProgram.setComputeUnitLimit({ units: COMPUTE_UNITS }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: microLamportsPerCu }),
    ...instructions,
  ];
  const { blockhash } = await connection.getLatestBlockhash("confirmed");
  const message = new TransactionMessage({
    payerKey: wallet,
    recentBlockhash: blockhash,
    instructions: allInstructions,
  }).compileToV0Message();
  const unsigned = new VersionedTransaction(message);
  const bytes = unsigned.serialize();
  assert(bytes.length <= 1232, `Built transaction too large: ${bytes.length} bytes.`);

  const topLevelPrograms = message.compiledInstructions.map(
    (instruction) => message.staticAccountKeys[instruction.programIdIndex].toBase58(),
  );
  assert(topLevelPrograms.includes(expectedProgram), `Official SDK transaction does not invoke expected program ${expectedProgram}.`);
  assert(!topLevelPrograms.includes("FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe"), "Unexpected PumpPortal router in native transaction.");

  return {
    ok: true,
    transaction: Buffer.from(bytes).toString("base64"),
    pool: route,
    expectedProgram,
    requestedLamports: amountLamports.toString(),
    requestedTokenRaw: amountRaw.toString(),
    topLevelPrograms,
  };
}

if (process.argv.includes("--check")) {
  await checkRuntime();
} else {
  let rpcUrl = "";
  try {
    const input = await readInput();
    rpcUrl = String(input.rpcUrl || "");
    const result = await build(input);
    process.stdout.write(JSON.stringify(result) + "\n");
  } catch (error) {
    process.stderr.write(sanitizeError(error, rpcUrl) + "\n");
    process.exitCode = 2;
  }
}
