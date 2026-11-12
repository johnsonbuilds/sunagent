"use client";

import {
  useI18n,
  type LocalePreference,
} from "@/lib/i18n";

import { LanguageIcon, RestartIcon } from "./Icons";

interface SettingsPanelProps {
  onRestart: () => void;
}

export default function SettingsPanel({ onRestart }: SettingsPanelProps) {
  const { t, preference, setPreference } = useI18n();

  return (
    <div className="panel">
      <div className="sidebar-head">
        <span className="sidebar-label">{t("settings")}</span>
      </div>
      <div className="settings-body">
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
          onClick={onRestart}
        >
          <RestartIcon />
          <span className="settings-item-text">
            <span className="settings-item-title">{t("restart")}</span>
            <span className="settings-item-desc">
              {t("restartDescription")}
            </span>
          </span>
        </button>
      </div>
    </div>
  );
}
