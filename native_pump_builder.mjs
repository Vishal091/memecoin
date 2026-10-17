import { ComputeBudgetProgram, Connection, PublicKey, TransactionMessage, VersionedTransaction } from "@solana/web3.js";
import BN from "bn.js";
import {
  OnlinePumpSdk,
  getBuyTokenAmountFromSolAmount,
  getSellSolAmountFromTokenAmount,
} from "@pump-fun/pump-sdk";
import {
  OnlinePumpAmmSdk,
  PUMP_AMM_SDK,
  canonicalPumpPoolPda,
} from "@pump-fun/pump-swap-sdk";

const PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
const PUMP_FEE_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ";
const WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112";
const ZERO_PUBKEY = "11111111111111111111111111111111";
const COMPUTE_UNITS = 400_000;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function publicKeyText(value) {
  if (value && typeof value.toBase58 === "function") return value.toBase58();
  return value ? String(value) : "";
}

function sanitizeError(error, rpcUrl) {
  let message = String(error?.stack || error?.message || error || "Unknown native builder error");
  if (rpcUrl) message = message.split(rpcUrl).join("[configured RPC URL]");
  return message.slice(0, 1800);
}

async function readInput() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  return JSON.parse(input);
}

async function checkRuntime() {
  assert(typeof OnlinePumpSdk === "function", "Missing OnlinePumpSdk export from @pump-fun/pump-sdk");
  assert(typeof getBuyTokenAmountFromSolAmount === "function", "Missing buy quote helper from @pump-fun/pump-sdk");
  assert(typeof getSellSolAmountFromTokenAmount === "function", "Missing sell quote helper from @pump-fun/pump-sdk");
  assert(typeof OnlinePumpAmmSdk === "function", "Missing OnlinePumpAmmSdk export from @pump-fun/pump-swap-sdk");
  assert(typeof canonicalPumpPoolPda === "function", "Missing canonicalPumpPoolPda export from @pump-fun/pump-swap-sdk");
  assert(typeof PUMP_AMM_SDK?.buyQuoteInput === "function", "Missing PumpSwap buyQuoteInput builder");
  assert(typeof PUMP_AMM_SDK?.sellBaseInput === "function", "Missing PumpSwap sellBaseInput builder");
  process.stdout.write(JSON.stringify({ ok: true, builder: "native-pump-sdk", router: "not-used" }) + "\n");
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
  const sdk = new OnlinePumpSdk(connection);
  const slippagePercent = slippageBps / 100;

  // Read the curve state first; completion decides the protocol-native route.
  const curveState = action === "buy"
    ? await sdk.fetchBuyState(mint, wallet)
    : await sdk.fetchSellState(mint, wallet);
  const curve = curveState.bondingCurve;
  assert(curve, "Pump bonding-curve state was not returned");

  let instructions;
  let route;
  let expectedProgram;
  if (!curve.complete) {
    const curveQuoteMint = publicKeyText(curve.quoteMint);
    assert(
      !curveQuoteMint || curveQuoteMint === ZERO_PUBKEY || curveQuoteMint === WRAPPED_SOL_MINT,
      "This executor currently supports only SOL-quoted Pump bonding curves; refusing a non-SOL quote mint.",
    );
    const [global, feeConfig, mintSupplyResponse] = await Promise.all([
      sdk.fetchGlobal(),
      sdk.fetchFeeConfig(),
      connection.getTokenSupply(mint, "confirmed"),
    ]);
    const mintSupply = new BN(mintSupplyResponse.value.amount);
    assert(mintSupply.gt(new BN(0)), "Mint supply is zero; cannot price curve trade.");

    if (action === "buy") {
      // buyInstructions expresses slippage as percent and makes an exact-out buy.
      // Reduce the quote target so its slippage ceiling remains within the requested
      // entry budget rather than multiplying the risk cap by (1 + slippage).
      const quoteLamports = amountLamports * 10_000n / BigInt(10_000 + slippageBps);
      assert(quoteLamports > 0n, "Trade size is too small after slippage allowance.");
      const quoteAmount = new BN(quoteLamports.toString());
      const tokensOut = getBuyTokenAmountFromSolAmount({
        global,
        feeConfig,
        mintSupply,
        bondingCurve: curve,
        amount: quoteAmount,
        quoteMint: new PublicKey(WRAPPED_SOL_MINT),
      });
      assert(tokensOut.gt(new BN(0)), "Pump quote returned zero tokens.");
      instructions = await sdk.buyInstructions({
        ...curveState,
        global,
        mint,
        user: wallet,
        amount: tokensOut,
        solAmount: quoteAmount,
        slippage: slippagePercent,
      });
    } else {
      const tokenAmount = new BN(amountRaw.toString());
      const expectedSol = getSellSolAmountFromTokenAmount({
        global,
        feeConfig,
        mintSupply,
        bondingCurve: curve,
        amount: tokenAmount,
        quoteMint: new PublicKey(WRAPPED_SOL_MINT),
      });
      assert(expectedSol.gt(new BN(0)), "Pump sell quote returned zero SOL.");
      instructions = await sdk.sellInstructions({
        ...curveState,
        global,
        mint,
        user: wallet,
        amount: tokenAmount,
        solAmount: expectedSol,
        slippage: slippagePercent,
      });
    }
    route = "pump";
    expectedProgram = PUMP_PROGRAM_ID;
  } else {
    // A graduated curve trades through its canonical PumpSwap pool. The SDK
    // constructs the PumpSwap instruction directly; no PumpPortal router is used.
    const poolAddress = canonicalPumpPoolPda(mint);
    const onlineAmm = new OnlinePumpAmmSdk(connection);
    const swapState = await onlineAmm.swapSolanaState(poolAddress, wallet);
    const quoteMint = publicKeyText(swapState?.pool?.quoteMint);
    assert(quoteMint === WRAPPED_SOL_MINT,
      "This executor currently supports only WSOL-quoted canonical PumpSwap pools; refusing a non-SOL quote mint.");

    if (action === "buy") {
      instructions = await PUMP_AMM_SDK.buyQuoteInput(
        swapState,
        new BN(amountLamports.toString()),
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

  assert(Array.isArray(instructions) && instructions.length > 0, "Native SDK returned no transaction instructions.");
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

  return {
    ok: true,
    transaction: Buffer.from(bytes).toString("base64"),
    pool: route,
    expectedProgram,
    requestedLamports: amountLamports.toString(),
    requestedTokenRaw: amountRaw.toString(),
    // For audit only; Python independently checks the compiled message.
    topLevelPrograms: message.compiledInstructions.map((ix) => message.staticAccountKeys[ix.programIdIndex].toBase58()),
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
