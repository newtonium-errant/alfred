import Head from 'next/head';
import { Layout } from '../components/Layout';
import { EmptyState } from '../components/EmptyState';
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card';
import { display, subtle } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// FE-1 shell. Proves the design system renders end-to-end: melon tokens (page
// wash, card, focus ring), the Layout header, Card slots, EmptyState, and the
// typography scale. The live chat thread + composer land in FE-2 — until then
// this is the warm "nothing here yet" resting state, not a blank page
// (intentionally-left-blank: the absence is an explicit, friendly signal).
export default function ChatPage() {
  return (
    <>
      <Head>
        <title>Chat · {INSTANCE_NAME}</title>
      </Head>
      <Layout>
        <h1 className={display}>Chat</h1>
        <p className={`mt-1 ${subtle}`}>
          A vault-grounded conversation with {INSTANCE_NAME}.
        </p>

        <Card className="mt-6">
          <CardHeader>
            <CardTitle>Conversation</CardTitle>
          </CardHeader>
          <CardContent>
            <EmptyState
              icon="💬"
              title="Nothing here yet"
              message="The chat thread arrives in the next build. The shell, design tokens, and primitives are in place."
              testId="chat-empty"
            />
          </CardContent>
        </Card>
      </Layout>
    </>
  );
}
