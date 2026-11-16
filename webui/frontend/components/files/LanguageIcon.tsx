"use client";

import { FileIcon } from "@/components/Icons";
import { languageFor } from "@/lib/fileIcons";

export default function LanguageIcon({ name }: { name: string }) {
  const language = languageFor(name);
  if (!language) return <FileIcon />;
  return (
    <span className={`files-lang ${language.tone}`} aria-hidden="true">
      {language.label}
    </span>
  );
}
