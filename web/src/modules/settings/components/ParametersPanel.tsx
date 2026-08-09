"use client";

/**
 * Parameters tab: global money parameters — minimum
 * share capital, registration fee, minimum monthly contribution,
 * maximum member exposure, dormancy period, financial year end, exit
 * notice period and exit fee.
 *
 * WYSIWYG per-tab write with the loaded `version`; blank clears a key.
 * Amounts are decimal STRINGS validated by shape only and submitted
 * verbatim (no client-side money math).
 */
import { useState, type FormEvent } from "react";
import { Card } from "@genesis/design-system";
import { FormField } from "@/modules/forms/FormField";
import { fromApiError, mergeFieldErrors, type FieldErrors } from "@/modules/forms/form-errors";
import { usePermissions } from "@/modules/authz/usePermissions";
import { can } from "@/modules/authz/schemas";
import type { UpdateSettingsInput } from "../api";
import {
  AMOUNT_RE,
  FY_MONTHS,
  parseOptionalDecimal,
  parseOptionalInt,
  type Settings,
} from "../schemas";
import { SettingsSaveControls, useSettingsSaveFlow } from "./SettingsSaveFlow";
import styles from "./Settings.module.css";

const AMOUNT_MSG = "Amount like 15000 or 15000.50 (max 2dp)";

export function ParametersPanel({
  settings,
  editing,
}: Readonly<{ settings: Settings; editing: boolean }>) {
  const permissions = usePermissions();
  const mayEdit = can(permissions.data, "settings", "edit");
  const flow = useSettingsSaveFlow("parameters");

  const [shareCapital, setShareCapital] = useState(settings.min_share_capital ?? "");
  const [registrationFee, setRegistrationFee] = useState(settings.registration_fee ?? "");
  const [monthlyContribution, setMonthlyContribution] = useState(
    settings.min_monthly_contribution ?? "",
  );
  const [memberExposure, setMemberExposure] = useState(settings.max_member_exposure ?? "");
  const [dormancyMonths, setDormancyMonths] = useState(
    settings.dormancy_period_months === null ? "" : String(settings.dormancy_period_months),
  );
  const [fyEndMonth, setFyEndMonth] = useState(
    settings.financial_year_end_month === null ? "" : String(settings.financial_year_end_month),
  );
  const [exitNoticeDays, setExitNoticeDays] = useState(
    settings.exit_notice_period_days === null ? "" : String(settings.exit_notice_period_days),
  );
  const [exitFee, setExitFee] = useState(settings.exit_fee ?? "");
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});

  const fieldErrors = mergeFieldErrors(clientErrors, fromApiError(flow.save.error));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (flow.save.isPending) return;

    const errors: Record<string, string> = {};
    const pShareCapital = parseOptionalDecimal(shareCapital, AMOUNT_RE, AMOUNT_MSG);
    if (!pShareCapital.ok) errors["min_share_capital"] = pShareCapital.message;
    const pRegistrationFee = parseOptionalDecimal(registrationFee, AMOUNT_RE, AMOUNT_MSG);
    if (!pRegistrationFee.ok) errors["registration_fee"] = pRegistrationFee.message;
    const pMonthlyContribution = parseOptionalDecimal(monthlyContribution, AMOUNT_RE, AMOUNT_MSG);
    if (!pMonthlyContribution.ok) errors["min_monthly_contribution"] = pMonthlyContribution.message;
    const pMemberExposure = parseOptionalDecimal(memberExposure, AMOUNT_RE, AMOUNT_MSG);
    if (!pMemberExposure.ok) errors["max_member_exposure"] = pMemberExposure.message;
    const pDormancyMonths = parseOptionalInt(dormancyMonths, 1, 120);
    if (!pDormancyMonths.ok) errors["dormancy_period_months"] = pDormancyMonths.message;
    const pExitNoticeDays = parseOptionalInt(exitNoticeDays, 0, 365);
    if (!pExitNoticeDays.ok) errors["exit_notice_period_days"] = pExitNoticeDays.message;
    const pExitFee = parseOptionalDecimal(exitFee, AMOUNT_RE, AMOUNT_MSG);
    if (!pExitFee.ok) errors["exit_fee"] = pExitFee.message;
    const pFyEndMonth = parseOptionalInt(fyEndMonth, 1, 12);
    if (!pFyEndMonth.ok) errors["financial_year_end_month"] = pFyEndMonth.message;

    if (Object.keys(errors).length > 0) {
      setClientErrors(errors);
      return;
    }
    setClientErrors({});

    const input: UpdateSettingsInput = {
      version: settings.version,
      min_share_capital: pShareCapital.ok ? pShareCapital.value : null,
      registration_fee: pRegistrationFee.ok ? pRegistrationFee.value : null,
      min_monthly_contribution: pMonthlyContribution.ok ? pMonthlyContribution.value : null,
      max_member_exposure: pMemberExposure.ok ? pMemberExposure.value : null,
      dormancy_period_months: pDormancyMonths.ok ? pDormancyMonths.value : null,
      financial_year_end_month: pFyEndMonth.ok ? pFyEndMonth.value : null,
      exit_notice_period_days: pExitNoticeDays.ok ? pExitNoticeDays.value : null,
      exit_fee: pExitFee.ok ? pExitFee.value : null,
    };
    flow.requestSave(input);
  }

  return (
    <form onSubmit={submit} noValidate>
      <Card>
        <h2 className={styles.sectionTitle}>Global parameters</h2>
        <div className={styles.fieldsGrid}>
          <FormField
            id="param-share-capital"
            label="Minimum share capital (KES)"
            error={fieldErrors["min_share_capital"]}
            hint="Blank = not configured."
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="decimal"
                maxLength={19}
                value={shareCapital}
                disabled={!mayEdit}
                onChange={(event) => setShareCapital(event.target.value)}
              />
            )}
          </FormField>
          <FormField
            id="param-registration-fee"
            label="Registration fee (KES)"
            error={fieldErrors["registration_fee"]}
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="decimal"
                maxLength={19}
                value={registrationFee}
                disabled={!mayEdit}
                onChange={(event) => setRegistrationFee(event.target.value)}
              />
            )}
          </FormField>
          <FormField
            id="param-monthly-contribution"
            label="Min. monthly deposit (KES)"
            error={fieldErrors["min_monthly_contribution"]}
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="decimal"
                maxLength={19}
                value={monthlyContribution}
                disabled={!mayEdit}
                onChange={(event) => setMonthlyContribution(event.target.value)}
              />
            )}
          </FormField>
          <FormField
            id="param-member-exposure"
            label="Max member exposure (KES)"
            error={fieldErrors["max_member_exposure"]}
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="decimal"
                maxLength={19}
                value={memberExposure}
                disabled={!mayEdit}
                onChange={(event) => setMemberExposure(event.target.value)}
              />
            )}
          </FormField>
          <FormField
            id="param-dormancy-months"
            label="Dormancy period (months)"
            error={fieldErrors["dormancy_period_months"]}
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="numeric"
                maxLength={3}
                value={dormancyMonths}
                disabled={!mayEdit}
                onChange={(event) => setDormancyMonths(event.target.value)}
              />
            )}
          </FormField>
          <FormField
            id="param-fy-end"
            label="Financial year end"
            error={fieldErrors["financial_year_end_month"]}
          >
            {(control) => (
              <select
                {...control}
                className={styles.select}
                value={fyEndMonth}
                disabled={!mayEdit}
                onChange={(event) => setFyEndMonth(event.target.value)}
              >
                <option value="">Not configured</option>
                {FY_MONTHS.map((label, index) => (
                  <option key={label} value={String(index + 1)}>
                    {label}
                  </option>
                ))}
              </select>
            )}
          </FormField>
          <FormField
            id="param-exit-notice"
            label="Exit notice period (days)"
            error={fieldErrors["exit_notice_period_days"]}
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="numeric"
                maxLength={3}
                value={exitNoticeDays}
                disabled={!mayEdit}
                onChange={(event) => setExitNoticeDays(event.target.value)}
              />
            )}
          </FormField>
          <FormField
            id="param-exit-fee"
            label="Exit fee (KES)"
            error={fieldErrors["exit_fee"]}
          >
            {(control) => (
              <input
                {...control}
                className={styles.input}
                inputMode="decimal"
                maxLength={19}
                value={exitFee}
                disabled={!mayEdit}
                onChange={(event) => setExitFee(event.target.value)}
              />
            )}
          </FormField>
        </div>
        <SettingsSaveControls
          flow={flow}
          mayEdit={mayEdit && editing}
          buttonLabel="Save parameters"
          confirmTitle="Apply parameters"
          confirmPhrase="parameters"
          settings={settings}
        >
          <div className={styles.formNote}>
            These parameters gate registrations, deposits, exposure checks,
            dormancy and member exit from the moment they are saved. Blank
            fields CLEAR their stored value.
          </div>
        </SettingsSaveControls>
      </Card>
    </form>
  );
}
