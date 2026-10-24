'use client';

import React, { useEffect, useRef, useState, useCallback, Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import {
  Bot,
  SendHorizontal,
  User,
  Sparkles,
  TrendingUp,
  Newspaper,
  TrainTrack,
  LogOut,
  HelpCircle,
  AlertCircle,
  RefreshCw,
  Menu,
  Loader2,
  ThumbsUp,
  ThumbsDown,
} from 'lucide-react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { cn } from '@/lib/utils';
import { authFetch, API_BASE, type User as AuthUser } from '@/lib/auth';
import {
  ApiError,
  hideConversation,
  listConversations,
  openConversation,
  type ConversationSummary,
} from '@/lib/conversations';
import { ConversationSidebar } from '@/components/ConversationSidebar';

interface Message {
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
  /** Set on answers from /chat this session; lets the user rate the answer. */
  turnId?: string;
  feedback?: 1 | -1;
}

const starterPrompts = [
  {
    icon: TrendingUp,
    title: 'Indian Markets',
    prompt: 'What is the live price, change, and overall trend of NIFTY 50 today?',
    badge: 'Markets',
  },
  {
    icon: Newspaper,
    title: 'Verified Headlines',
    prompt: 'Show me the top latest news headlines in India today.',
    badge: 'News',
  },
  {
    icon: TrainTrack,
    title: 'IRCTC Railways',
    prompt: 'What is the running status of Vande Bharat Express?',
    badge: 'Railways',
  },
  {
    icon: HelpCircle,
    title: 'Market Overview',
    prompt: 'Give me a quick breakdown of major stock market movers in India.',
    badge: 'Analysis',
  },
];

const INITIAL_WELCOME: Message = {
  role: 'assistant',
  content:
    '👋 **VoxPath Terminal Active.** Ask me anything about live Indian stock markets, verified news headlines, or IRCTC railway status.',
};

/**
 * Links in answers (news sources, mostly) open in a new tab so the chat stays
 * put. noopener/noreferrer keep the opened site from controlling this page.
 */
const markdownComponents: Components = {
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

/** fetch() itself failed: nothing answered at API_BASE (as opposed to an error reply). */
class BackendUnreachableError extends Error {
  constructor() {
    super('Unable to reach the VoxPath backend.');
  }
}

const MESSAGES_KEY = 'voxpath_chat_messages';
const THREAD_KEY = 'voxpath_thread_id';
const PAGE_SIZE = 20; // chats per sidebar page
const MAX_PAGE = 200; // the backend's largest allowed limit

/**
 * Mint a conversation id. crypto.randomUUID() needs a secure context, which
 * localhost and https satisfy but a plain-http LAN address does not.
 */
function newThreadId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `t-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

type ChatProps = { user: AuthUser; onSignOut: () => void };

function ChatContent({ user, onSignOut }: ChatProps) {
  const searchParams = useSearchParams();
  const [messages, setMessages] = useState<Message[]>([INITIAL_WELCOME]);
  const [isThinking, setIsThinking] = useState(false);
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null);
  const [status, setStatus] = useState<'Ready' | 'Processing' | 'Reconnecting'>('Ready');
  const [error, setError] = useState<string | null>(null);
  const [draftMessage, setDraftMessage] = useState('');

  // Sidebar: the user's saved chats, and which one is open.
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [openingChat, setOpeningChat] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false); // phone drawer

  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isBusy = useRef(false);
  const threadIdRef = useRef<string | null>(null);
  // Bumped on every chat switch: a slow reply for a chat the user has already
  // left must not overwrite the one now on screen.
  const openSeq = useRef(0);

  // The thread id identifies this browser's conversation; the transcript itself
  // lives in Postgres behind it. Resolved on first use, not during render.
  const getThreadId = useCallback(() => {
    if (threadIdRef.current) return threadIdRef.current;
    let id: string | null = null;
    try {
      id = localStorage.getItem(THREAD_KEY);
      if (!id) {
        id = newThreadId();
        localStorage.setItem(THREAD_KEY, id);
      }
    } catch {
      // Private mode or blocked storage: fall back to a per-tab thread.
      id = id || newThreadId();
    }
    threadIdRef.current = id;
    setActiveId(id);
    return id;
  }, []);

  // Load a page of the sidebar list. offset 0 replaces the list; more appends.
  const fetchConversations = useCallback(
    async (offset: number, limit: number) => {
      setListLoading(true);
      setListError(null);
      try {
        const page = await listConversations(limit, offset);
        setConversations((prev) =>
          offset === 0 ? page : [...prev, ...page.filter((chat) => !prev.some((p) => p.id === chat.id))]
        );
        setHasMore(page.length === limit);
      } catch (listFailure) {
        if (listFailure instanceof ApiError && listFailure.status === 401) {
          onSignOut();
          return;
        }
        setListError(listFailure instanceof Error ? listFailure.message : 'Could not load chats.');
      } finally {
        setListLoading(false);
      }
    },
    [onSignOut]
  );

  // Re-read the list from the top, keeping as many chats as are already shown.
  const refreshConversations = () =>
    fetchConversations(0, Math.min(Math.max(PAGE_SIZE, conversations.length), MAX_PAGE));

  const startNewChat = useCallback(() => {
    openSeq.current++; // drop any chat still loading
    setMessages([INITIAL_WELCOME]);
    setOpeningChat(false);
    setError(null);
    setSidebarOpen(false);
    // Emptying the transcript is not enough: without a fresh thread id the
    // agent still replays the old conversation from Postgres.
    threadIdRef.current = null;
    setActiveId(null);
    try {
      localStorage.removeItem(MESSAGES_KEY);
      localStorage.removeItem(THREAD_KEY);
    } catch {
      // Ignore
    }
  }, []);

  // Make `id` the current chat and show its messages from the server.
  const showConversation = useCallback(
    async (id: string) => {
      const seq = ++openSeq.current;
      threadIdRef.current = id;
      setActiveId(id);
      try {
        localStorage.setItem(THREAD_KEY, id);
      } catch {
        // Ignore
      }
      setOpeningChat(true);
      setError(null);
      try {
        const chat = await openConversation(id);
        if (seq !== openSeq.current) return;
        setMessages([INITIAL_WELCOME, ...chat.messages]);
      } catch (openFailure) {
        if (seq !== openSeq.current) return;
        if (openFailure instanceof ApiError && openFailure.status === 401) {
          onSignOut();
        } else if (openFailure instanceof ApiError && openFailure.status === 404) {
          // Hidden, or no longer this user's: it can't be continued.
          setConversations((prev) => prev.filter((chat) => chat.id !== id));
          startNewChat();
        } else if (!(openFailure instanceof ApiError && openFailure.status === 0)) {
          // Backend unreachable (status 0) is already shown in the header; the
          // browser's copy of the chat stays on screen meanwhile.
          setError(`Couldn't open that chat: ${openFailure instanceof Error ? openFailure.message : 'unknown error'}`);
        }
      } finally {
        if (seq === openSeq.current) setOpeningChat(false);
      }
    },
    [onSignOut, startNewChat]
  );

  // On load: fetch the chat list, and refresh the open chat from the server
  // (the browser's saved copy shows instantly, then the server's replaces it).
  useEffect(() => {
    void fetchConversations(0, PAGE_SIZE);
    let savedId: string | null = null;
    try {
      savedId = localStorage.getItem(THREAD_KEY);
    } catch {
      // Ignore
    }
    if (savedId) void showConversation(savedId);
  }, [fetchConversations, showConversation]);

  const selectConversation = (id: string) => {
    setSidebarOpen(false);
    if (id === activeId && !openingChat) return;
    void showConversation(id);
  };

  const hideChat = async (id: string) => {
    const chat = conversations.find((c) => c.id === id);
    if (!window.confirm(`Hide "${chat?.title ?? 'this chat'}"? It will disappear from your chats.`)) return;
    try {
      await hideConversation(id);
    } catch (hideFailure) {
      if (hideFailure instanceof ApiError && hideFailure.status === 401) {
        onSignOut();
        return;
      }
      // 404 means it is already hidden or gone: remove it from the list anyway.
      if (!(hideFailure instanceof ApiError && hideFailure.status === 404)) {
        setError(`Couldn't hide that chat: ${hideFailure instanceof Error ? hideFailure.message : 'unknown error'}`);
        return;
      }
    }
    setConversations((prev) => prev.filter((c) => c.id !== id));
    // A hidden chat can't take new messages, so leave it if it is open.
    if (id === threadIdRef.current) startNewChat();
  };

  // Load local storage history on mount
  useEffect(() => {
    try {
      const saved = localStorage.getItem(MESSAGES_KEY);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed) && parsed.length > 0) {
          setMessages(parsed);
        }
      }
    } catch {
      // Ignore localStorage errors
    }
  }, []);

  // Save messages to local storage whenever they change
  useEffect(() => {
    if (messages.length > 0) {
      try {
        localStorage.setItem(MESSAGES_KEY, JSON.stringify(messages));
      } catch {
        // Ignore quota/storage errors
      }
    }
  }, [messages]);

  // Check backend health
  const checkHealth = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/health`, { signal: AbortSignal.timeout(3000) });
      setBackendOnline(res.ok);
    } catch {
      setBackendOnline(false);
    }
  }, []);

  useEffect(() => {
    void checkHealth();
    const timer = window.setInterval(checkHealth, 6000);
    return () => window.clearInterval(timer);
  }, [checkHealth]);

  // Auto scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, isThinking]);

  // Handle URL query parameter if present
  useEffect(() => {
    const query = searchParams.get('q');
    if (query && query.trim()) {
      setDraftMessage(query.trim());
    }
  }, [searchParams]);

  // Auto-resize textarea
  const adjustTextareaHeight = () => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 140)}px`;
    }
  };

  const handleTextChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setDraftMessage(e.target.value);
    adjustTextareaHeight();
  };

  // Thumbs up/down on an answer. Clicking the chosen thumb again clears the vote.
  // Shown at once; put back if the server doesn't save it.
  const rateAnswer = async (turnId: string, value: 1 | -1) => {
    const previous = messages.find((m) => m.turnId === turnId)?.feedback;
    const next = previous === value ? undefined : value;
    const setFeedback = (feedback: 1 | -1 | undefined) =>
      setMessages((prev) => prev.map((m) => (m.turnId === turnId ? { ...m, feedback } : m)));
    setFeedback(next);
    try {
      const res = await authFetch('/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turn_id: turnId, rating: next ?? 0 }),
      });
      if (res.status === 401) {
        onSignOut();
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setFeedback(previous);
      setError("Couldn't save your rating. Please try again.");
    }
  };

  const sendTextMessage = async (customPrompt?: string) => {
    const textToSend = (customPrompt || draftMessage).trim();
    if (!textToSend || isBusy.current || openingChat) return;

    isBusy.current = true;
    setDraftMessage('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }

    const userMsg: Message = { role: 'user', content: textToSend };
    setMessages((prev) => [...prev, userMsg]);
    setIsThinking(true);
    setStatus('Processing');
    setError(null);

    try {
      let res: Response;
      let data;
      for (let attempt = 0; ; attempt++) {
        try {
          res = await authFetch('/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              message: textToSend,
              thread_id: getThreadId(),
            }),
          });
        } catch {
          // fetch itself failed: the backend is not reachable at all.
          throw new BackendUnreachableError();
        }

        // Error pages from a crashed server or a proxy are not JSON; don't let the
        // parse error replace the real problem.
        data = await res.json().catch(() => null);

        // The database was briefly unreachable before anything ran (the backend says
        // so with retryable=true): wait as told and resend once, automatically.
        if (res.status === 503 && data?.retryable === true && attempt === 0) {
          const waitSeconds = Math.min(Number(res.headers.get('Retry-After')) || 5, 10);
          setStatus('Reconnecting');
          await new Promise((resolve) => setTimeout(resolve, waitSeconds * 1000));
          setStatus('Processing');
          continue;
        }
        break;
      }

      if (res.status === 401) {
        onSignOut();
        throw new Error('Your session expired. Please sign in again.');
      }

      if (!res.ok) {
        const message =
          typeof data?.error === 'string'
            ? data.error
            : typeof data?.detail === 'string'
              ? data.detail
              : `Backend request failed (HTTP ${res.status}).`;
        throw new Error(message);
      }

      if (!data) {
        throw new Error('The backend sent a response that could not be read.');
      }

      if (data.text) {
        const assistantMsg: Message = {
          role: 'assistant',
          content: data.text,
          turnId: typeof data.turn_id === 'string' ? data.turn_id : undefined,
        };
        setMessages((prev) => [...prev, assistantMsg]);
      }
    } catch (backendError) {
      console.error('Backend fetch error:', backendError);
      const msg = backendError instanceof Error ? backendError.message : 'Something went wrong.';
      setError(msg);
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content:
            backendError instanceof BackendUnreachableError
              ? `⚠️ **Connection Error**: ${msg}\n\nMake sure the backend Python service is running on \`${API_BASE}\`.`
              : `⚠️ ${msg}`,
        },
      ]);
    } finally {
      isBusy.current = false;
      setIsThinking(false);
      setStatus('Ready');
      // A new chat appears in the sidebar, and the open one moves to the top.
      void refreshConversations();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void sendTextMessage();
    }
  };

  return (
    <div className="relative flex h-[100dvh] w-full overflow-hidden pt-16 font-sans text-slate-200 bg-background">
      <ConversationSidebar
        conversations={conversations}
        activeId={activeId}
        loading={listLoading && conversations.length === 0}
        error={listError}
        hasMore={hasMore}
        disabled={isThinking}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onSelect={selectConversation}
        onHide={(id) => void hideChat(id)}
        onNewChat={startNewChat}
        onRetry={() => void refreshConversations()}
        onLoadMore={() => void fetchConversations(conversations.length, PAGE_SIZE)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
      {/* Sub-header status bar */}
      <header className="flex h-12 flex-shrink-0 items-center justify-between border-b border-white/5 bg-white/[0.02] px-4 md:px-10">
        <div className="flex items-center gap-2 text-xs font-semibold text-white">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Show your conversations"
            className="-ml-1 mr-1 rounded-md p-1.5 text-slate-300 hover:bg-white/5 hover:text-white md:hidden"
          >
            <Menu size={16} aria-hidden="true" />
          </button>
          <Sparkles size={14} className="text-accent" aria-hidden="true" />
          <span className="hidden sm:inline">Intelligence Session</span>
        </div>

        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-2.5 py-1">
            <div
              className={cn(
                'h-2 w-2 rounded-full',
                backendOnline === true ? 'bg-emerald-400 animate-pulse' : backendOnline === false ? 'bg-rose-500' : 'bg-amber-400 animate-ping'
              )}
              aria-hidden="true"
            />
            <span className="text-[10px] font-bold uppercase tracking-wider text-slate-300">
              {backendOnline === true ? 'Backend Online' : backendOnline === false ? 'Backend Offline' : 'Checking'}
            </span>
          </div>

          <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400">{status}</span>

          <span
            className="hidden md:inline max-w-[180px] truncate text-[11px] text-slate-400"
            title={user.email ? `${user.username} (${user.email})` : user.username}
          >
            {user.username}
          </span>
          <button
            onClick={onSignOut}
            aria-label="Sign out"
            title="Sign out"
            className="flex items-center gap-1 text-[11px] font-medium text-slate-300 transition-colors hover:text-rose-400"
          >
            <LogOut size={13} aria-hidden="true" />
            <span className="hidden sm:inline">Sign out</span>
          </button>
        </div>
      </header>

      {/* Messages Scroll Area with custom visual scrollbar */}
      <div
        ref={scrollRef}
        aria-live="polite"
        aria-label="Chat messages transcript"
        className="mx-auto w-full max-w-3xl flex-1 space-y-6 overflow-y-auto px-4 sm:px-6 py-6 custom-scrollbar"
      >
        {openingChat && (
          <div className="flex items-center justify-center gap-2 text-xs text-slate-400" role="status">
            <Loader2 size={13} className="animate-spin" aria-hidden="true" />
            Loading chat…
          </div>
        )}

        {messages.map((msg, i) => (
          <div
            key={i}
            className={cn('flex animate-fade-in gap-3.5', msg.role === 'user' ? 'flex-row-reverse' : 'flex-row')}
          >
            <div
              className={cn(
                'mt-1 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-xl text-xs shadow-md',
                msg.role === 'assistant'
                  ? 'bg-gradient-to-br from-violet-600 to-cyan-500 text-white'
                  : 'border border-white/10 bg-white/10 text-slate-200'
              )}
              aria-hidden="true"
            >
              {msg.role === 'assistant' ? <Bot size={16} /> : <User size={16} />}
            </div>

            <div
              className={cn(
                'max-w-[85%] rounded-2xl px-5 py-3.5 text-sm leading-relaxed shadow-sm',
                msg.role === 'assistant'
                  ? 'surface-card text-slate-100 border border-white/10'
                  : 'bg-gradient-to-br from-violet-600/90 to-indigo-600/90 text-white'
              )}
            >
              {msg.role === 'assistant' ? (
                <div className="prose prose-invert max-w-none text-sm leading-relaxed prose-p:my-1.5 prose-ul:my-1.5 prose-ol:my-1.5 prose-li:my-0.5 prose-headings:text-white prose-headings:font-bold prose-strong:text-white prose-code:text-cyan-300 prose-code:bg-white/10 prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-a:text-cyan-400">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                    {msg.content}
                  </ReactMarkdown>
                  {msg.turnId && (
                    <div className="not-prose mt-2 flex gap-1" role="group" aria-label="Rate this answer">
                      {([1, -1] as const).map((value) => {
                        const Icon = value === 1 ? ThumbsUp : ThumbsDown;
                        const chosen = msg.feedback === value;
                        return (
                          <button
                            key={value}
                            type="button"
                            onClick={() => void rateAnswer(msg.turnId!, value)}
                            aria-label={value === 1 ? 'Good answer' : 'Bad answer'}
                            aria-pressed={chosen}
                            title={value === 1 ? 'Good answer' : 'Bad answer'}
                            className={cn(
                              'rounded-md p-1.5 transition-colors hover:bg-white/10',
                              chosen ? (value === 1 ? 'text-emerald-400' : 'text-rose-400') : 'text-slate-500 hover:text-slate-200'
                            )}
                          >
                            <Icon size={13} aria-hidden="true" fill={chosen ? 'currentColor' : 'none'} />
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              ) : (
                <p className="whitespace-pre-wrap">{msg.content}</p>
              )}
            </div>
          </div>
        ))}

        {/* Starter Suggestion Cards when only welcome message exists */}
        {messages.length <= 1 && !openingChat && (
          <div className="mt-8 space-y-4 animate-fade-in" aria-label="Suggested starter queries">
            <div className="text-center text-xs font-semibold uppercase tracking-wider text-slate-400">
              Suggested Starter Queries
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {starterPrompts.map((sp) => {
                const Icon = sp.icon;
                return (
                  <button
                    key={sp.title}
                    onClick={() => void sendTextMessage(sp.prompt)}
                    aria-label={`Ask: ${sp.prompt}`}
                    className="command-chip flex flex-col items-start p-4 rounded-2xl text-left border border-white/10 bg-white/5 hover:bg-white/10 hover:border-white/20 transition-all group"
                  >
                    <div className="flex items-center justify-between w-full mb-2">
                      <div className="flex items-center gap-2 text-xs font-bold text-white">
                        <Icon size={15} className="text-accent" aria-hidden="true" />
                        <span>{sp.title}</span>
                      </div>
                      <span className="text-[10px] font-semibold text-slate-300 bg-white/5 border border-white/10 px-2 py-0.5 rounded-full">
                        {sp.badge}
                      </span>
                    </div>
                    <p className="text-xs text-slate-300 group-hover:text-white line-clamp-2">{sp.prompt}</p>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {isThinking && (
          <div className="flex animate-pulse items-center gap-3 pl-11" aria-label="Assistant thinking">
            <div className="flex gap-1.5" aria-hidden="true">
              <div className="h-2 w-2 rounded-full bg-accent" />
              <div className="h-2 w-2 rounded-full bg-accent opacity-60" />
              <div className="h-2 w-2 rounded-full bg-accent opacity-30" />
            </div>
            <span className="text-[10px] font-bold uppercase tracking-[0.25em] text-accent/90">
              Executing Agent Tools...
            </span>
          </div>
        )}
      </div>

      {/* Input Area */}
      <div className="flex-shrink-0 px-4 sm:px-6 pb-6 pt-2">
        <div className="surface-card glow relative mx-auto max-w-3xl rounded-2xl border border-white/15 p-2 bg-[#0c0c1c]/90">
          {error && (
            <div className="absolute -top-11 left-0 right-0 text-center">
              <span className="inline-flex items-center gap-1.5 rounded-full border border-rose-500/30 bg-rose-950/80 px-4 py-1.5 text-xs font-semibold text-rose-300 backdrop-blur-md shadow-lg">
                <AlertCircle size={13} aria-hidden="true" />
                {error}
              </span>
            </div>
          )}

          <form
            className="flex items-end gap-3 px-3 py-1.5"
            onSubmit={(e) => {
              e.preventDefault();
              void sendTextMessage();
            }}
          >
            <Sparkles size={18} className="mb-3 text-slate-400 transition-colors group-focus-within:text-accent" aria-hidden="true" />

            <textarea
              ref={textareaRef}
              rows={1}
              value={draftMessage}
              onChange={handleTextChange}
              onKeyDown={handleKeyDown}
              aria-label="Type your message prompt"
              placeholder="Ask about Indian stock quotes, railway status, or news headlines... (Shift+Enter for newline)"
              className="flex-1 resize-none border-none bg-transparent text-sm font-medium text-slate-100 outline-none placeholder:text-slate-400 py-1.5 max-h-36 custom-scrollbar"
            />

            <button
              type="submit"
              aria-label="Send prompt"
              disabled={!draftMessage.trim() || isThinking || isBusy.current || openingChat}
              className="btn-gradient flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl disabled:opacity-30 disabled:grayscale transition-transform active:scale-95"
            >
              <SendHorizontal size={17} aria-hidden="true" />
            </button>
          </form>
        </div>
        <div className="mt-2 text-center text-[11px] text-slate-400">
          VoxPath calls live tools for Indian markets, IRCTC, and news. Powered by MCP Agent.
        </div>
      </div>
      </div>
    </div>
  );
}

export const ChatInterface = ({ user, onSignOut }: ChatProps) => {
  return (
    <Suspense
      fallback={
        <div className="flex h-[100dvh] w-full items-center justify-center text-slate-300 gap-2">
          <RefreshCw size={18} className="animate-spin text-accent" aria-hidden="true" />
          <span>Loading Terminal Interface...</span>
        </div>
      }
    >
      <ChatContent user={user} onSignOut={onSignOut} />
    </Suspense>
  );
};
