"use client";

/**
 * Interest tab: loan interest method/basis, penalty
 * rules, deposit/dividend/rebate rates and the tiered loan-rate bands.
 *
 * WYSIWYG per-tab write: the full displayed field set is submitted with
 * the loaded `version`; blank clears a key (the API's explicit-null
 * contract). Rates and band amounts are decimal STRINGS validated by
 * shape only — the client never compares or computes money values;
 * band ordering/contiguity is the server's verdict (422 field
 * messages). Band minimums are DERIVED by string copy from the
 * previous band's upper bound, so gaps/overlaps are impossible to
 * enter by construction.
 */
import { useState, type FormEvent } from "react";
import { Button, Card } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import type { UpdateSettingsInput } from "../api";
import {
  AMOUNT_RE,
  BASIS_LABELS,
  CHARGED_ON_LABELS,
  LOAN_INTEREST_BASES,
  LOAN_INTEREST_METHODS,
  METHOD_LABELS,
  PENALTY_CHARGED_ON,
  RATE_RE,
  parseOptionalDecimal,
  parseOptionalInt,
  type Settings,
} from "../schemas";
import { SettingsSaveControls, useSettingsSaveFlow } from "./SettingsSaveFlow";
import grid from "@/modules/layout/grid.module.css";
import styles from "./Settings.module.css";

interface BandRow {
  /** Upper bound (KES decimal string); "" = open-ended (last band only). */
  upper: string;
  /** Rate % for the band (decimal string). */
  rate: string;
}

const RATE_MSG = "Decimal like 12 or 12.5 (max 2dp)";
const AMOUNT_MSG = "Amount like 100000 or 100000.50 (max 2dp)";

export function InterestPanel({
  settings,
  editing,
}: Readonly<{ settings: Settings; editing: boolean }>) {
  const permissions = usePermissions();
  const mayEdit = can(permissions.data, "settings", "edit");
  const flow = useSettingsSaveFlow("interest");

  // The boundary REJECTS unknown enum vocabulary (W57-2), so the
  // selects prefill the stored value directly — a degraded-to-blank
  // value that would be silently NULLED on save can no longer exist.
  const [method, setMethod] = useState<string>(settings.loan_interest_method ?? "");
  const [basis, setBasis] = useState<string>(settings.loan_interest_basis ?? "");
  const [penaltyRate, setPenaltyRate] = useState(settings.penalty_rate_pct_per_month ?? "");
  const [penaltyGrace, setPenaltyGrace] = useState(
    settings.penalty_grace_days === null ? "" : String(settings.penalty_grace_days),
  );
  const [chargedOn, setChargedOn] = useState<string>(settings.penalty_charged_on ?? "");
  const [depositRate, setDepositRate] = useState(
    settings.deposit_interest_annual_rate_pct ?? "",
  );
  const [dividendRate, setDividendRate] = useState(settings.dividend_rate_pct ?? "");
  const [rebateRate, setRebateRate] = useState(settings.deposit_rebate_rate_pct ?? "");
  const [bands, setBands] = useState<BandRow[]>(
    (settings.loan_rate_bands ?? []).map((band) => ({
      upper: band.max_amount ?? "",
      rate: band.rate_pct,
    })),
  );
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});

  const fieldErrors = mergeFieldErrors(clientErrors, fromApiError(flow.save.error));

  function setBand(index: number, patch: Partial<BandRow>) {
    setBands((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (flow.save.isPending) return;

    const errors: Record<string, string> = {};
    const pPenaltyRate = parseOptionalDecimal(penaltyRate, RATE_RE, RATE_MSG);
    if (!pPenaltyRate.ok) errors["penalty_rate_pct_per_month"] = pPenaltyRate.message;
    const pPenaltyGrace = parseOptionalInt(penaltyGrace, 0, 365);
    if (!pPenaltyGrace.ok) errors["penalty_grace_days"] = pPenaltyGrace.message;
    const pDepositRate = parseOptionalDecimal(depositRate, RATE_RE, RATE_MSG);
    if (!pDepositRate.ok) errors["deposit_interest_annual_rate_pct"] = pDepositRate.message;
    const pDividendRate = parseOptionalDecimal(dividendRate, RATE_RE, RATE_MSG);
    if (!pDividendRate.ok) errors["dividend_rate_pct"] = pDividendRate.message;
    const pRebateRate = parseOptionalDecimal(rebateRate, RATE_RE, RATE_MSG);
    if (!pRebateRate.ok) errors["deposit_rebate_rate_pct"] = pRebateRate.message;

    bands.forEach((row, index) => {
      const isLast = index === bands.length - 1;
      const upper = row.upper.trim();
      const rate = row.rate.trim();
      if (upper === "") {
        if (!isLast) {
          errors[`loan_rate_bands.${index}.upper`] = "Only the last band may be open-ended";
        }
      } else if (!AMOUNT_RE.test(upper)) {
        errors[`loan_rate_bands.${index}.upper`] = AMOUNT_MSG;
      }
      if (!RATE_RE.test(rate)) {
        errors[`loan_rate_bands.${index}.rate`] = RATE_MSG;
      }
    });

    if (Object.keys(errors).length > 0) {
      setClientErrors(errors);
      return;
    }
    setClientErrors({});

    const input: UpdateSettingsInput = {
      version: settings.version,
      loan_interest_method:
        method === "" ? null : (method as UpdateSettingsInput["loan_interest_method"]),
      loan_interest_basis:
        basis === "" ? null : (basis as UpdateSettingsInput["loan_interest_basis"]),
      penalty_rate_pct_per_month: pPenaltyRate.ok ? pPenaltyRate.value : null,
      penalty_grace_days: pPenaltyGrace.ok ? pPenaltyGrace.value : null,
      penalty_charged_on:
        chargedOn === "" ? null : (chargedOn as UpdateSettingsInput["penalty_charged_on"]),
      deposit_interest_annual_rate_pct: pDepositRate.ok ? pDepositRate.value : null,
      dividend_rate_pct: pDividendRate.ok ? pDividendRate.value : null,
      deposit_rebate_rate_pct: pRebateRate.ok ? pRebateRate.value : null,
      // Band minimums derive from the previous band's upper bound by
      // STRING COPY (first band starts at 0) — contiguous by
      // construction, no client-side amount math anywhere.
      loan_rate_bands:
        bands.length === 0
          ? null
          : bands.map((row, index) => ({
              min_amount: index === 0 ? "0" : bands[index - 1]!.upper.trim(),
              max_amount: row.upper.trim() === "" ? null : row.upper.trim(),
              rate_pct: row.rate.trim(),
            })),
    };
    flow.requestSave(input);
  }

  return (
    <form onSubmit={submit} noValidate>
      <div className={grid.cards4}>
        <Card className={grid.half}>
          <h2 className={styles.sectionTitle}>Loan interest</h2>
          <div className={styles.fieldsGrid}>
            <FormField
              id="interest-method"
              label="Calculation method"
              error={fieldErrors["loan_interest_method"]}
              hint="Stored config — the P6 engine applies it server-side."
            >
              {(control) => (
                <select
                  {...control}
                  className={styles.select}
                  value={method}
                  disabled={!mayEdit}
                  onChange={(event) => setMethod(event.target.value)}
                >
                  <option value="">Not configured</option>
                  {LOAN_INTEREST_METHODS.map((value) => (
                    <option key={value} value={value}>
                      {METHOD_LABELS[value]}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField
              id="interest-basis"
              label="Interest basis"
              error={fieldErrors["loan_interest_basis"]}
            >
              {(control) => (
                <select
                  {...control}
                  className={styles.select}
                  value={basis}
                  disabled={!mayEdit}
                  onChange={(event) => setBasis(event.target.value)}
                >
                  <option value="">Not configured</option>
                  {LOAN_INTEREST_BASES.map((value) => (
                    <option key={value} value={value}>
                      {BASIS_LABELS[value]}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
          </div>

          <div className={styles.subhead}>Penalty on arrears</div>
          <div className={styles.fieldsGrid}>
            <FormField
              id="interest-penalty-rate"
              label="Penalty rate (%/mo)"
              error={fieldErrors["penalty_rate_pct_per_month"]}
              hint="Blank = not configured."
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  inputMode="decimal"
                  maxLength={6}
                  value={penaltyRate}
                  disabled={!mayEdit}
                  onChange={(event) => setPenaltyRate(event.target.value)}
                />
              )}
            </FormField>
            <FormField
              id="interest-penalty-grace"
              label="Penalty grace (days)"
              error={fieldErrors["penalty_grace_days"]}
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  inputMode="numeric"
                  maxLength={3}
                  value={penaltyGrace}
                  disabled={!mayEdit}
                  onChange={(event) => setPenaltyGrace(event.target.value)}
                />
              )}
            </FormField>
          </div>
          <FormField
            id="interest-charged-on"
            label="Charged on"
            error={fieldErrors["penalty_charged_on"]}
          >
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={chargedOn}
                disabled={!mayEdit}
                onChange={(event) => setChargedOn(event.target.value)}
              >
                <option value="">Not configured</option>
                {PENALTY_CHARGED_ON.map((value) => (
                  <option key={value} value={value}>
                    {CHARGED_ON_LABELS[value]}
                  </option>
                ))}
              </select>
            )}
          </FormField>

          <div className={styles.subhead}>Deposits &amp; shares</div>
          <div className={styles.fieldsGrid}>
            <FormField
              id="interest-deposit-rate"
              label="Interest on deposits (% p.a.)"
              error={fieldErrors["deposit_interest_annual_rate_pct"]}
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  inputMode="decimal"
                  maxLength={6}
                  value={depositRate}
                  disabled={!mayEdit}
                  onChange={(event) => setDepositRate(event.target.value)}
                />
              )}
            </FormField>
            <FormField
              id="interest-dividend-rate"
              label="Dividend on shares (% p.a.)"
              error={fieldErrors["dividend_rate_pct"]}
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  inputMode="decimal"
                  maxLength={6}
                  value={dividendRate}
                  disabled={!mayEdit}
                  onChange={(event) => setDividendRate(event.target.value)}
                />
              )}
            </FormField>
            <FormField
              id="interest-rebate-rate"
              label="Deposit rebate (% p.a.)"
              error={fieldErrors["deposit_rebate_rate_pct"]}
            >
              {(control) => (
                <input
                  {...control}
                  className={styles.input}
                  inputMode="decimal"
                  maxLength={6}
                  value={rebateRate}
                  disabled={!mayEdit}
                  onChange={(event) => setRebateRate(event.target.value)}
                />
              )}
            </FormField>
          </div>
        </Card>

        <Card className={grid.half}>
          <h2 className={styles.sectionTitle}>Tiered rates (by amount)</h2>
          {fieldErrors["loan_rate_bands"] !== undefined && (
            <div className={styles.fieldError} role="alert">
              {fieldErrors["loan_rate_bands"]}
            </div>
          )}
          {bands.length === 0 && (
            <div className={styles.formNote}>
              No tiered bands configured — loans price on the product rate.
            </div>
          )}
          {bands.map((row, index) => (
            <div key={index}>
              <div className={styles.subhead}>
                Band {index + 1} — from{" "}
                {index === 0 ? "0" : bands[index - 1]!.upper.trim() || "(previous upper)"}
              </div>
              <div className={styles.fieldsGrid}>
                <FormField
                  id={`rate-band-${index}-upper`}
                  label={`Band ${index + 1} upper bound (KES)`}
                  error={fieldErrors[`loan_rate_bands.${index}.upper`]}
                  hint={
                    index === bands.length - 1 ? "Blank = open-ended top band." : undefined
                  }
                >
                  {(control) => (
                    <input
                      {...control}
                      className={styles.input}
                      inputMode="decimal"
                      maxLength={19}
                      value={row.upper}
                      disabled={!mayEdit}
                      onChange={(event) => setBand(index, { upper: event.target.value })}
                    />
                  )}
                </FormField>
                <FormField
                  id={`rate-band-${index}-rate`}
                  label={`Band ${index + 1} rate (%)`}
                  error={fieldErrors[`loan_rate_bands.${index}.rate`]}
                >
                  {(control) => (
                    <input
                      {...control}
                      className={styles.input}
                      inputMode="decimal"
                      maxLength={6}
                      value={row.rate}
                      disabled={!mayEdit}
                      onChange={(event) => setBand(index, { rate: event.target.value })}
                    />
                  )}
                </FormField>
              </div>
              {mayEdit && (
                <div className={styles.bandActions}>
                  <Button
                    type="button"
                    onClick={() => setBands((prev) => prev.filter((_, i) => i !== index))}
                  >
                    Remove band {index + 1}
                  </Button>
                </div>
              )}
            </div>
          ))}
          {mayEdit && (
            <div className={styles.bandActions}>
              <Button
                type="button"
                onClick={() => setBands((prev) => [...prev, { upper: "", rate: "" }])}
              >
                + Add band
              </Button>
            </div>
          )}
          <div className={styles.formNote}>
            Bands are contiguous from 0 by construction (each band starts at
            the previous upper bound). Ordering and bounds are enforced by
            the server.
          </div>
        </Card>

        <div className={grid.wide}>
          <SettingsSaveControls
            flow={flow}
            mayEdit={mayEdit && editing}
            buttonLabel="Save interest rules"
            confirmTitle="Apply interest rules"
            confirmPhrase="interest"
            settings={settings}
          >
            <div className={styles.formNote}>
              Interest, penalty and dividend parameters drive money postings
              from the moment they are saved (accruals read config at run
              time; past postings never change). Blank fields CLEAR their
              stored value.
            </div>
          </SettingsSaveControls>
        </div>
      </div>
    </form>
  );
}
