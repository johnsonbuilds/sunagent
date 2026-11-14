"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type Locale = "en" | "zh";

/** "auto" follows the browser; a concrete locale is an explicit choice. */
export type LocalePreference = "auto" | Locale;

export const LOCALE_STORAGE_KEY = "sunagent-locale";

const en = {
  statusRunning: "Running",
  statusReconnecting: "Reconnecting…",
  statusError: "Error",
  runFailed: "The run failed",
  newConversation: "New conversation",
  untitled: "Untitled",
  thisConversation: "this conversation",
  toggleSidebar: "Toggle sidebar",
  toggleTheme: "Toggle theme",
  newConversationShortcut: "New conversation (Ctrl+K)",
  modelPickerTitle: "Select model",
  harnessPickerTitle: "Select harness",
  harnessLockedHint:
    "The harness is fixed once the conversation starts. Open a new conversation to change it.",
  nextTurnHint: "Applies from the next turn",
  composerPlaceholder: "Message SunAgent…",
  composerHint: "Enter sends · Shift+Enter adds a line",
  send: "Send",
  stop: "Stop",
  conversations: "Conversations",
  filterConversations: "Filter conversations…",
  deleteConversation: "Delete conversation",
  conversationTitle: "Conversation title",
  deleteConversationConfirm: 'Delete "{title}"?',
  timeNow: "now",
  timeMinutes: "{count}m ago",
  timeHours: "{count}h ago",
  timeDays: "{count}d ago",
  emptyTitle: "What should we work on?",
  emptyBody:
    "SunAgent runs tools, reads files, and executes commands. Ask a question to get started.",
  suggestionFiles: "What files are in the current directory?",
  suggestionStructure:
    "Show me the project structure and explain what each part does.",
  suggestionTests: "Run the test suite and summarise the failures.",
  suggestionReadme: "Read the README and give me a short orientation.",
  thinking: "Thinking",
  toolFallback: "tool",
  exitCode: "exit {code}",
  navSidebarLabel: "Sidebar navigation",
  navChat: "Chat",
  navBoard: "Board",
  settings: "Settings",
  restart: "Restart",
  restartDescription: "Run ./run.sh restart in the current directory",
  language: "Language",
  languageDescription: "Follows the browser by default",
  languageAuto: "Follow browser",
  languageEnglish: "English",
  languageChinese: "中文",
  restartDialogTitle: "Restart service",
  restartDialogMessage:
    "Restart the SunAgent service? This page will briefly disconnect.",
  restartConfirm: "Restart",
  restartBusy: "Restarting…",
  restartingToast: "Restarting the service, please wait…",
  restartTimeoutToast: "The restart timed out. Refresh the page manually.",
  close: "Close",
  defaultConfirm: "Confirm",
  defaultCancel: "Cancel",
  defaultBusy: "Working…",
  boardTitle: "Task board",
  boardNewTask: "New task",
  boardSearch: "Filter tasks…",
  boardHideDone: "Hide done",
  boardColumns: "Columns",
  boardLoading: "Loading board…",
  boardLoadFailed: "Could not load the board.",
  boardRetry: "Retry",
  boardEmptyColumn: "Drop a task here",
  boardTaskCount: "{count} tasks",
  boardRefresh: "Refresh board",
  boardColumnBacklog: "Backlog",
  boardColumnTodo: "To do",
  boardColumnDoing: "Doing",
  boardColumnReview: "Review",
  boardColumnDone: "Done",
  boardPriority: "Priority",
  boardPriorityLow: "Low",
  boardPriorityNormal: "Normal",
  boardPriorityHigh: "High",
  boardPriorityUrgent: "Urgent",
  boardStatus: "Status",
  boardTitleField: "Title",
  boardTitleRequired: "A title is required",
  boardDescription: "Description",
  boardTags: "Tags",
  boardTagsPlaceholder: "backend, bug",
  boardConversation: "Conversation",
  boardOpenConversation: "Open conversation",
  boardCreateConversation: "Create conversation",
  boardUnlinked: "No conversation",
  boardRunning: "Running",
  boardMoveTo: "Move to",
  boardEditTask: "Edit task",
  boardDeleteTask: "Delete task",
  boardDeleteTaskConfirm: 'Delete "{title}"? Its conversation will be kept.',
  boardSave: "Save",
  boardDescriptionRequired: "A description is required",
  boardRunFailed: "Run failed:",
  boardQueue: "Queue position",
  boardPauseAutoRun: "Pause auto-run",
  boardResumeAutoRun: "Resume auto-run",
  boardAutoRunPaused: "Auto-run paused",
  boardModel: "Model",
  boardHarness: "Harness",
  boardDefaultModel: "Service default",
  boardDefaultHarness: "Service default",
} as const;

export type MessageKey = keyof typeof en;

export type TFunction = (
  key: MessageKey,
  params?: Record<string, string | number>,
) => string;

const zh: Record<MessageKey, string> = {
  statusRunning: "运行中",
  statusReconnecting: "正在重新连接…",
  statusError: "出错",
  runFailed: "运行失败",
  newConversation: "新会话",
  untitled: "未命名",
  thisConversation: "此会话",
  toggleSidebar: "切换侧边栏",
  toggleTheme: "切换主题",
  newConversationShortcut: "新会话 (Ctrl+K)",
  modelPickerTitle: "选择模型",
  harnessPickerTitle: "选择执行框架",
  harnessLockedHint: "会话开始后执行框架不可更改，如需更换请新建会话。",
  nextTurnHint: "将在下一轮生效",
  composerPlaceholder: "给 SunAgent 发送消息…",
  composerHint: "Enter 发送 · Shift+Enter 换行",
  send: "发送",
  stop: "停止",
  conversations: "会话",
  filterConversations: "筛选会话…",
  deleteConversation: "删除会话",
  conversationTitle: "会话标题",
  deleteConversationConfirm: "删除“{title}”？",
  timeNow: "刚刚",
  timeMinutes: "{count} 分钟前",
  timeHours: "{count} 小时前",
  timeDays: "{count} 天前",
  emptyTitle: "今天想做点什么？",
  emptyBody: "SunAgent 可以运行工具、读取文件并执行命令。问个问题就能开始。",
  suggestionFiles: "当前目录里有哪些文件？",
  suggestionStructure: "展示项目结构，并解释每一部分是做什么的。",
  suggestionTests: "运行测试套件并总结失败的用例。",
  suggestionReadme: "读一下 README，给我一个简短的入门说明。",
  thinking: "思考中",
  toolFallback: "工具",
  exitCode: "退出码 {code}",
  navSidebarLabel: "侧边栏导航",
  navChat: "聊天",
  navBoard: "看板",
  settings: "设置",
  restart: "重启",
  restartDescription: "在当前目录执行 ./run.sh restart",
  language: "语言",
  languageDescription: "默认跟随浏览器",
  languageAuto: "跟随浏览器",
  languageEnglish: "English",
  languageChinese: "中文",
  restartDialogTitle: "重启服务",
  restartDialogMessage: "确定要重启 SunAgent 服务吗？当前页面会短暂断开连接。",
  restartConfirm: "确定重启",
  restartBusy: "正在重启…",
  restartingToast: "正在重启服务，请稍候…",
  restartTimeoutToast: "服务重启超时，请手动刷新页面。",
  close: "关闭",
  defaultConfirm: "确定",
  defaultCancel: "取消",
  defaultBusy: "处理中…",
  boardTitle: "任务看板",
  boardNewTask: "新建任务",
  boardSearch: "筛选任务…",
  boardHideDone: "隐藏已完成",
  boardColumns: "列",
  boardLoading: "正在加载看板…",
  boardLoadFailed: "看板加载失败。",
  boardRetry: "重试",
  boardEmptyColumn: "拖拽任务到这里",
  boardTaskCount: "{count} 个任务",
  boardRefresh: "刷新看板",
  boardColumnBacklog: "待整理",
  boardColumnTodo: "待办",
  boardColumnDoing: "进行中",
  boardColumnReview: "待验收",
  boardColumnDone: "已完成",
  boardPriority: "优先级",
  boardPriorityLow: "低",
  boardPriorityNormal: "普通",
  boardPriorityHigh: "高",
  boardPriorityUrgent: "紧急",
  boardStatus: "状态",
  boardTitleField: "标题",
  boardTitleRequired: "标题不能为空",
  boardDescription: "描述",
  boardTags: "标签",
  boardTagsPlaceholder: "backend, bug",
  boardConversation: "关联对话",
  boardOpenConversation: "打开对话",
  boardCreateConversation: "创建对话",
  boardUnlinked: "未关联对话",
  boardRunning: "运行中",
  boardMoveTo: "移动到",
  boardEditTask: "编辑任务",
  boardDeleteTask: "删除任务",
  boardDeleteTaskConfirm: "删除“{title}”？关联对话会保留。",
  boardSave: "保存",
  boardDescriptionRequired: "描述不能为空",
  boardRunFailed: "执行失败：",
  boardQueue: "队列位置",
  boardPauseAutoRun: "暂停自动执行",
  boardResumeAutoRun: "恢复自动执行",
  boardAutoRunPaused: "自动执行已暂停",
  boardModel: "模型",
  boardHarness: "执行框架",
  boardDefaultModel: "使用服务默认",
  boardDefaultHarness: "使用服务默认",
};

const dictionaries: Record<Locale, Record<MessageKey, string>> = { en, zh };

/** Resolve the browser's preferred language to a supported locale. */
export function detectLocale(): Locale {
  if (typeof navigator === "undefined") return "en";
  const languages =
    navigator.languages && navigator.languages.length
      ? navigator.languages
      : [navigator.language];
  for (const language of languages) {
    const normalized = (language || "").toLowerCase();
    if (normalized.startsWith("zh")) return "zh";
    if (normalized.startsWith("en")) return "en";
  }
  return "en";
}

function readPreference(): LocalePreference {
  try {
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    if (stored === "auto" || stored === "en" || stored === "zh") {
      return stored;
    }
  } catch {
    /* private mode */
  }
  return "auto";
}

function resolveLocale(preference: LocalePreference): Locale {
  return preference === "auto" ? detectLocale() : preference;
}

export function formatMessage(
  locale: Locale,
  key: MessageKey,
  params?: Record<string, string | number>,
): string {
  let message = dictionaries[locale][key] ?? dictionaries.en[key] ?? key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      message = message.split(`{${name}}`).join(String(value));
    }
  }
  return message;
}

interface I18nValue {
  locale: Locale;
  preference: LocalePreference;
  setPreference: (preference: LocalePreference) => void;
  t: TFunction;
}

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  // Start in English so the static export hydrates without a mismatch,
  // then adopt the stored/browser preference once mounted.
  const [preference, setPreferenceState] = useState<LocalePreference>("auto");
  const [locale, setLocale] = useState<Locale>("en");

  useEffect(() => {
    const stored = readPreference();
    setPreferenceState(stored);
    setLocale(resolveLocale(stored));
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
  }, [locale]);

  // While following the browser, react to the user changing its language.
  useEffect(() => {
    if (preference !== "auto") return;
    const sync = () => setLocale(detectLocale());
    window.addEventListener("languagechange", sync);
    return () => window.removeEventListener("languagechange", sync);
  }, [preference]);

  const setPreference = useCallback((next: LocalePreference) => {
    setPreferenceState(next);
    setLocale(resolveLocale(next));
    try {
      window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
    } catch {
      /* private mode */
    }
  }, []);

  const t = useCallback(
    (key: MessageKey, params?: Record<string, string | number>) =>
      formatMessage(locale, key, params),
    [locale],
  );

  const value = useMemo(
    () => ({ locale, preference, setPreference, t }),
    [locale, preference, setPreference, t],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) {
    throw new Error("useI18n must be used inside an I18nProvider");
  }
  return value;
}
