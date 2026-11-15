"use client";

import ConfirmDialog from "@/components/ConfirmDialog";
import { LanguageIcon, RestartIcon } from "@/components/Icons";
import { useI18n, type LocalePreference } from "@/lib/i18n";
import { useRestart } from "@/lib/useRestart";

export default function SettingsPage() {
  const { t, preference, setPreference } = useI18n();
  const restart = useRestart();

  return (
    <div className="settings-page">
      <div className="settings-page-inner">
        <header className="settings-page-head">
          <h1 className="settings-page-title">{t("settings")}</h1>
        </header>

        <section className="settings-section">
          <div className="settings-item settings-item-static">
            <LanguageIcon />
            <span className="settings-item-text">
              <span className="settings-item-title">{t("language")}</span>
              <span className="settings-item-desc">
                {t("languageDescription")}
              </span>
            </span>
            <select
              className="settings-select"
              id="selectLanguage"
              aria-label={t("language")}
              value={preference}
              onChange={(event) =>
                setPreference(event.target.value as LocalePreference)
              }
            >
              <option value="auto">{t("languageAuto")}</option>
              <option value="en">{t("languageEnglish")}</option>
              <option value="zh">{t("languageChinese")}</option>
            </select>
          </div>

          <button
            className="settings-item"
            id="btnRestart"
            type="button"
            onClick={restart.request}
          >
            <RestartIcon />
            <span className="settings-item-text">
              <span className="settings-item-title">{t("restart")}</span>
              <span className="settings-item-desc">
                {t("restartDescription")}
              </span>
            </span>
          </button>
        </section>
      </div>

      <ConfirmDialog
        open={restart.open}
        title={t("restartDialogTitle")}
        message={t("restartDialogMessage")}
        confirmText={t("restartConfirm")}
        cancelText={t("defaultCancel")}
        busyText={t("restartBusy")}
        tone="danger"
        busy={restart.busy}
        onConfirm={() => void restart.confirm()}
        onCancel={restart.cancel}
      />
    </div>
  );
}
