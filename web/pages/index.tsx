import Head from 'next/head';
import { Layout } from '../components/Layout';
import { ChatThread } from '../components/chat/ChatThread';
import { Composer } from '../components/chat/Composer';
import { Button } from '../components/ui/button';
import { useChat } from '../lib/algernon/useChat';
import { display, subtle } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// The chat surface (M1, non-streaming). Resumes the active session on load,
// shows a typing indicator while a turn is in flight, and renders a warm empty
// state before the first message. Errors surface in a danger banner (danger-red
// is reserved for true system errors) but leave the composer usable for a retry.
export default function ChatPage() {
  const { messages, status, error, sending, send, newChat } = useChat();
  const booting = status === 'booting';

  return (
    <>
      <Head>
        <title>Chat · {INSTANCE_NAME}</title>
      </Head>
      <Layout>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className={display}>Chat</h1>
            <p className={`mt-1 ${subtle}`}>
              A vault-grounded conversation with {INSTANCE_NAME}.
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            data-testid="new-chat"
            onClick={() => void newChat()}
            disabled={booting || sending}
          >
            New chat
          </Button>
        </div>

        <div className="mt-6 flex min-h-[55vh] flex-col gap-4">
          <div className="flex-1">
            {booting ? (
              // Intentionally-left-blank: an explicit loading signal, not a blank pane.
              <p data-testid="chat-booting" className={subtle}>
                Loading the conversation…
              </p>
            ) : (
              <ChatThread messages={messages} sending={sending} />
            )}
          </div>

          {error && (
            <p
              role="alert"
              data-testid="chat-error"
              className="rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
            >
              {error}
            </p>
          )}

          <Composer onSend={(t) => void send(t)} disabled={booting || sending} />
        </div>
      </Layout>
    </>
  );
}
