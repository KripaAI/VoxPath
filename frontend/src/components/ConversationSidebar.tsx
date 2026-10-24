'use client';

import React from 'react';
import { MessageSquare, Plus, Trash2, X, Loader2, AlertCircle } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { ConversationSummary } from '@/lib/conversations';

type SidebarProps = {
  conversations: ConversationSummary[];
  activeId: string | null;
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  /** Switching is paused while the agent is answering, so a reply can't land in the wrong chat. */
  disabled: boolean;
  /** Phone layout: whether the drawer is showing. Ignored on wider screens. */
  open: boolean;
  onClose: () => void;
  onSelect: (id: string) => void;
  onHide: (id: string) => void;
  onNewChat: () => void;
  onRetry: () => void;
  onLoadMore: () => void;
};

/** "Today", "Yesterday", "22 Oct", or "22 Oct 2025" for an earlier year. */
function dayLabel(iso: string): string {
  const date = new Date(iso);
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOfDay(new Date()) - startOfDay(date)) / 86_400_000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  return date.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    ...(date.getFullYear() !== new Date().getFullYear() ? { year: 'numeric' } : {}),
  });
}

/** The list of the user's saved chats, with New chat at the top. */
export function ConversationSidebar({
  conversations,
  activeId,
  loading,
  error,
  hasMore,
  disabled,
  open,
  onClose,
  onSelect,
  onHide,
  onNewChat,
  onRetry,
  onLoadMore,
}: SidebarProps) {
  return (
    <>
      {/* Phone: dim the chat behind the open drawer; tapping it closes the drawer. */}
      {open && (
        <div className="fixed inset-0 top-16 z-30 bg-black/60 md:hidden" onClick={onClose} aria-hidden="true" />
      )}

      <aside
        aria-label="Your conversations"
        className={cn(
          'fixed bottom-0 left-0 top-16 z-40 flex w-72 flex-col border-r border-white/10 bg-[#0a0a18] transition-transform duration-200',
          'md:static md:z-auto md:w-64 md:translate-x-0 md:bg-white/[0.015]',
          open ? 'translate-x-0' : '-translate-x-full'
        )}
      >
        <div className="flex items-center gap-2 p-3">
          <button
            type="button"
            onClick={onNewChat}
            disabled={disabled}
            className="flex flex-1 items-center justify-center gap-2 rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-white/10 disabled:opacity-40"
          >
            <Plus size={14} aria-hidden="true" />
            New chat
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close conversations"
            className="rounded-lg p-2 text-slate-400 hover:bg-white/5 hover:text-white md:hidden"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <div className="px-4 pb-2 text-[10px] font-bold uppercase tracking-wider text-slate-500">Chats</div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2 pb-3 custom-scrollbar">
          {conversations.map((chat) => {
            const active = chat.id === activeId;
            return (
              <div
                key={chat.id}
                className={cn(
                  'group flex items-center rounded-lg transition-colors',
                  active ? 'bg-accent/15' : 'hover:bg-white/5'
                )}
              >
                <button
                  type="button"
                  onClick={() => onSelect(chat.id)}
                  disabled={disabled && !active}
                  aria-current={active ? 'page' : undefined}
                  title={chat.title}
                  className="flex min-w-0 flex-1 items-start gap-2.5 px-2.5 py-2 text-left disabled:opacity-40"
                >
                  <MessageSquare
                    size={14}
                    className={cn('mt-0.5 flex-shrink-0', active ? 'text-accent' : 'text-slate-500')}
                    aria-hidden="true"
                  />
                  <span className="min-w-0">
                    <span className={cn('block truncate text-xs', active ? 'font-semibold text-white' : 'text-slate-200')}>
                      {chat.title}
                    </span>
                    <span className="block text-[10px] text-slate-500">{dayLabel(chat.last_used)}</span>
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => onHide(chat.id)}
                  disabled={disabled}
                  aria-label={`Hide chat: ${chat.title}`}
                  title="Hide chat"
                  className="mr-1 rounded-md p-1.5 text-slate-500 transition-colors hover:bg-rose-500/10 hover:text-rose-400 disabled:opacity-40 md:opacity-0 md:group-hover:opacity-100 md:focus:opacity-100"
                >
                  <Trash2 size={13} aria-hidden="true" />
                </button>
              </div>
            );
          })}

          {loading && (
            <div className="flex items-center gap-2 px-3 py-2 text-xs text-slate-400">
              <Loader2 size={13} className="animate-spin" aria-hidden="true" />
              Loading chats…
            </div>
          )}

          {!loading && error && (
            <div className="px-3 py-2 text-xs text-rose-300">
              <p className="flex items-start gap-1.5">
                <AlertCircle size={13} className="mt-px flex-shrink-0" aria-hidden="true" />
                Couldn&apos;t load chats.
              </p>
              <button type="button" onClick={onRetry} className="mt-1 font-semibold text-slate-200 underline hover:text-white">
                Retry
              </button>
            </div>
          )}

          {!loading && !error && conversations.length === 0 && (
            <p className="px-3 py-2 text-xs text-slate-500">No chats yet. Your conversations will appear here.</p>
          )}

          {!loading && !error && hasMore && (
            <button
              type="button"
              onClick={onLoadMore}
              className="w-full rounded-lg px-3 py-2 text-left text-xs font-medium text-slate-400 hover:bg-white/5 hover:text-white"
            >
              Load more
            </button>
          )}
        </nav>
      </aside>
    </>
  );
}
