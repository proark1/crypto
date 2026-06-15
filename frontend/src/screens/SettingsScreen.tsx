import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  fetchCampaignSettings,
  fetchTradingFees,
  updateCampaignSettings,
  updateTradingFees,
} from "../api/client";
import type { CampaignSettingsResponse, TradingFeesResponse } from "../api/types";
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
            error={fees === null || buyDraft === fees.buy_fee_percent ? null : buyError}
            disabled={fees === null}
            onChange={(next) => {
              setBuyDraft(next);
              setSaved(false);
            }}
          />
          <FeeField
            label="Sell fee"
            value={sellDraft}
            error={fees === null || sellDraft === fees.sell_fee_percent ? null : sellError}
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
      <CampaignSettingsCard />
    </div>
  );
}

/**
 * A live on/off switch for the §12.7 research-campaign loop. Self-contained: it
 * loads its own state and flips it through the API, mirroring the trading-fees
 * control above. The switch is honored within a cycle — no redeploy.
 */
function CampaignSettingsCard() {
  const [campaign, setCampaign] = useState<CampaignSettingsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      setCampaign(await fetchCampaignSettings());
    } catch (caught) {
      setLoadError(
        caught instanceof ApiError ? caught.message : "could not load campaign settings",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = useCallback(async () => {
    if (campaign === null) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      setCampaign(await updateCampaignSettings(!campaign.enabled));
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "could not update campaign settings",
      );
    } finally {
      setSaving(false);
    }
  }, [campaign]);

  return (
    <Card padding="lg">
      <SectionHeader
        title="Research campaigns"
        description="sweep, promote what beats the live settings out of sample, refine, repeat"
      />
      <Alert tone="info">
        When on, the bot runs research campaigns continuously — promoting only changes that
        clear the walk-forward bar against an untouched holdout, paper-only and reversible. It
        supersedes the single-sweep self-improver while it runs, and the switch takes effect
        within a cycle — no redeploy.
      </Alert>
      {loadError !== null && (
        <div className="mt-3">
          <Alert tone="error">{loadError}</Alert>
        </div>
      )}
      {campaign !== null && (
        <div className="mt-4 flex items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-zinc-800 dark:text-zinc-200">
              {campaign.enabled ? "Running campaigns" : "Off"}
            </p>
            <p className="mt-0.5 text-[12px] text-zinc-500 dark:text-zinc-400">
              up to {campaign.max_rounds} rounds or {campaign.max_hours}h per campaign, on{" "}
              {campaign.timeframe}
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={campaign.enabled}
            aria-label="Research campaigns"
            disabled={saving}
            onClick={() => {
              void toggle();
            }}
            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${
              campaign.enabled ? "bg-emerald-500" : "bg-zinc-300 dark:bg-zinc-700"
            }`}
          >
            <span
              className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
                campaign.enabled ? "translate-x-5" : "translate-x-0.5"
              }`}
            />
          </button>
        </div>
      )}
      {error !== null && (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
    </Card>
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
