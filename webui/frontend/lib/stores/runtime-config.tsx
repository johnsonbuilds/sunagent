"use client";

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { api } from "@/lib/api";
import type { HarnessOption, ModelOption, RuntimeConfig } from "@/lib/types";

import { useToast } from "./toast";

interface RuntimeConfigValue {
  models: ModelOption[];
  harnesses: HarnessOption[];
  defaultModel: string;
  defaultHarness: string;
  loaded: boolean;
}

const RuntimeConfigContext = createContext<RuntimeConfigValue | null>(null);

export function RuntimeConfigProvider({ children }: { children: ReactNode }) {
  const { showToast } = useToast();
  const [models, setModels] = useState<ModelOption[]>([]);
  const [harnesses, setHarnesses] = useState<HarnessOption[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [defaultHarness, setDefaultHarness] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;

    void (async () => {
      try {
        const config = await api<RuntimeConfig>("/api/config");
        if (cancelled) return;
        const modelList = config.models || [];
        const harnessList = config.harnesses || [];
        setModels(modelList);
        setHarnesses(harnessList);
        setDefaultModel(config.default_model || modelList[0]?.id || "");
        setDefaultHarness(config.default_harness || harnessList[0]?.id || "");
        if (config.error) showToast(config.error);
      } catch {
        if (cancelled) return;
        setModels([]);
        setHarnesses([]);
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [showToast]);

  const value = useMemo(
    () => ({ models, harnesses, defaultModel, defaultHarness, loaded }),
    [models, harnesses, defaultModel, defaultHarness, loaded],
  );

  return (
    <RuntimeConfigContext.Provider value={value}>
      {children}
    </RuntimeConfigContext.Provider>
  );
}

export function useRuntimeConfig(): RuntimeConfigValue {
  const value = useContext(RuntimeConfigContext);
  if (!value) {
    throw new Error(
      "useRuntimeConfig must be used inside RuntimeConfigProvider",
    );
  }
  return value;
}
