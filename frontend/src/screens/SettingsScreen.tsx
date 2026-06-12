import { useCallback, useEffect, useState } from "react";

import { ApiError, fetchTradingFees, updateTradingFees } from "../api/client";
import type { TradingFeesResponse } from "../api/types";
import { Alert, Button, Card, SectionHeader } from "../ui";

/** A fee draft is valid when it is a non-negative number no larger than 10%
 * (the backend's sanity cap of 1000 bps). Validating live lets the field flag
 * itself as the user types rather than only failing on save. Fees are a rate,
 * not money, so parsing the draft for validation is allowed. */
function feeDraftError(draft: string): string | null {
  const text = draft.trim();
  if (text === "" || Number.isNaN(Number(text))) {
    return "enter a number";
  }
  const value = Number(text);
  if (value < 0) {
    return "cannot be negative";
  }
  if (value > 10) {
    return "10% is the maximum";
  }
  return null;
}

/**
 * Global trading-fee settings: one buy fee and one sell fee, as percentages of
 * each fill's value, applied to every bot's future paper fills. Display and
 * input only — the exact percent → basis-point conversion happens on the
 * backend so no money math runs here.
 */
export function SettingsScreen() {
  const [fees, setFees] = useState<TradingFeesResponse | null>(null);
  const [buyDraft, setBuyDraft] = useState("");
  const [sellDraft, setSellDraft] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const current = await fetchTradingFees();
      setFees(current);
      setBuyDraft(current.buy_fee_percent);
      setSellDraft(current.sell_fee_percent);
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : "could not load settings");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const buyError = feeDraftError(buyDraft);
  const sellError = feeDraftError(sellDraft);
  const dirty =
    fees !== null && (buyDraft !== fees.buy_fee_percent || sellDraft !== fees.sell_fee_percent);

  const save = useCallback(async () => {
    if (buyError !== null || sellError !== null) {
      return;
    }
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      const updated = await updateTradingFees(buyDraft.trim(), sellDraft.trim());
      setFees(updated);
      setBuyDraft(updated.buy_fee_percent);
      setSellDraft(updated.sell_fee_percent);
      setSaved(true);
    } catch (error) {
      setSaveError(error instanceof ApiError ? error.message : "could not save fees");
    } finally {
      setSaving(false);
    }
  }, [buyDraft, sellDraft, buyError, sellError]);

  return (
    <div className="space-y-4">
      <Card padding="lg">
        <SectionHeader
          title="Trading fees"
          description="charged on every buy and sell, across all bots"
        />
        <Alert tone="info">
          These fees are folded into every bot&apos;s paper P&amp;L the moment they trade — the
          0.1% default mirrors a standard spot exchange. A change takes effect on the next fill;
          past trades keep the fee they already paid.
        </Alert>
        {loadError !== null && (
          <div className="mt-3">
            <Alert tone="error">{loadError}</Alert>
          </div>
        )}
        <form
          className="mt-4 space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <FeeField
            label="Buy fee"
            value={buyDraft}
            error={buyDraft === fees?.buy_fee_percent ? null : buyError}
            disabled={fees === null}
            onChange={(next) => {
              setBuyDraft(next);
              setSaved(false);
            }}
          />
          <FeeField
            label="Sell fee"
            value={sellDraft}
            error={sellDraft === fees?.sell_fee_percent ? null : sellError}
            disabled={fees === null}
            onChange={(next) => {
              setSellDraft(next);
              setSaved(false);
            }}
          />
          {saveError !== null && <Alert tone="error">{saveError}</Alert>}
          {saved && <Alert tone="success">Fees saved — effective on the next fill.</Alert>}
          <div className="flex items-center gap-3">
            <Button
              type="submit"
              variant="primary"
              disabled={
                fees === null || saving || !dirty || buyError !== null || sellError !== null
              }
            >
              {saving ? "saving…" : "save fees"}
            </Button>
            {dirty && (
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  // `dirty` implies fees is loaded, so TS narrows it non-null.
                  setBuyDraft(fees.buy_fee_percent);
                  setSellDraft(fees.sell_fee_percent);
                  setSaved(false);
                  setSaveError(null);
                }}
              >
                reset
              </Button>
            )}
          </div>
        </form>
      </Card>
    </div>
  );
}

/** One labelled percentage input with a trailing "%" and live error text. */
function FeeField(props: {
  label: string;
  value: string;
  error: string | null;
  disabled: boolean;
  onChange: (next: string) => void;
}) {
  return (
    <label className="block">
      <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
        {props.label}
      </span>
      <div className="mt-1 flex items-center gap-2">
        <input
          type="number"
          step="any"
          min="0"
          inputMode="decimal"
          value={props.value}
          disabled={props.disabled}
          aria-label={props.label}
          aria-invalid={props.error !== null}
          onChange={(event) => {
            props.onChange(event.target.value);
          }}
          className="w-32 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm text-zinc-900 disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100"
        />
        <span className="text-sm text-zinc-500 dark:text-zinc-400">% of each trade</span>
      </div>
      {props.error !== null && (
        <span className="mt-0.5 block text-[11px] text-red-600 dark:text-red-400">
          {props.error}
        </span>
      )}
    </label>
  );
}
