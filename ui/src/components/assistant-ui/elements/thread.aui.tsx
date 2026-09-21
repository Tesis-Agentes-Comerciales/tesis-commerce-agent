import {
  AuiIf,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  type TextMessagePartComponent,
  useAuiState,
} from "@assistant-ui/react"
import { ArrowDownIcon, ArrowUpIcon, SquareIcon } from "lucide-react"
import type { ComponentType, FC } from "react"

type ThreadComponents = {
  Welcome?: ComponentType
}

type ThreadProps = {
  components?: ThreadComponents
  autoFocus?: boolean
}

const DefaultWelcome: FC = () => (
  <h2 className="mb-6 text-center text-2xl tracking-[-0.025em]">¿Qué quieres comprar?</h2>
)

const TextPart: TextMessagePartComponent = ({ text }) => (
  <p className="whitespace-pre-wrap">{text}</p>
)

function ThreadMessage() {
  const role = useAuiState((state) => state.message.role)

  if (role === "user") {
    return (
      <MessagePrimitive.Root className="flex justify-end px-2">
        <div className="max-w-[75%] rounded-[22px] bg-[#0d0d0d] px-4 py-2.5 leading-6 text-white wrap-break-word">
          <MessagePrimitive.Parts components={{ Text: TextPart }} />
        </div>
      </MessagePrimitive.Root>
    )
  }

  return (
    <MessagePrimitive.Root className="px-2 leading-7 wrap-break-word">
      <MessagePrimitive.Parts components={{ Text: TextPart }} />
      <AuiIf condition={(state) => state.thread.isRunning}>
        <span className="animate-pulse" aria-label="El agente está respondiendo">
          ●
        </span>
      </AuiIf>
      <MessagePrimitive.Error>
        <ErrorPrimitive.Root className="mt-2 rounded-xl bg-red-50 p-3 text-sm text-red-700">
          <ErrorPrimitive.Message />
        </ErrorPrimitive.Root>
      </MessagePrimitive.Error>
    </MessagePrimitive.Root>
  )
}

function Composer({ autoFocus }: { autoFocus: boolean }) {
  return (
    <ComposerPrimitive.Root className="relative flex w-full items-end gap-1 rounded-[28px] border border-[#e5e5e5] bg-white p-2 focus-within:border-[#cfcfcf]">
      <ComposerPrimitive.Input
        placeholder="Describe lo que quieres comprar..."
        className="max-h-52 min-h-9 flex-1 resize-none bg-transparent py-1.5 pr-2 pl-3 text-base leading-6 outline-none placeholder:text-[#8e8e8e]"
        rows={1}
        autoFocus={autoFocus}
        enterKeyHint="send"
        aria-label="Mensaje de compra"
      />
      <AuiIf condition={(state) => !state.thread.isRunning}>
        <ComposerPrimitive.Send asChild>
          <button
            type="button"
            className="flex size-9 shrink-0 items-center justify-center rounded-full bg-[#0d0d0d] text-white transition-opacity hover:opacity-85 disabled:bg-[#999]"
            aria-label="Enviar mensaje"
          >
            <ArrowUpIcon className="size-4" aria-hidden />
          </button>
        </ComposerPrimitive.Send>
      </AuiIf>
      <AuiIf condition={(state) => state.thread.isRunning}>
        <ComposerPrimitive.Cancel asChild>
          <button
            type="button"
            className="flex size-9 shrink-0 items-center justify-center rounded-full bg-[#0d0d0d] text-white"
            aria-label="Detener respuesta"
          >
            <SquareIcon className="size-3.5 fill-current" aria-hidden />
          </button>
        </ComposerPrimitive.Cancel>
      </AuiIf>
    </ComposerPrimitive.Root>
  )
}

export function Thread({ components = {}, autoFocus = true }: ThreadProps) {
  const Welcome = components.Welcome ?? DefaultWelcome

  return (
    <ThreadPrimitive.Root className="flex h-full flex-col bg-white text-[#0d0d0d]">
      <ThreadPrimitive.Viewport className="relative flex flex-1 flex-col overflow-y-auto scroll-smooth">
        <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col px-4 pt-4">
          <AuiIf condition={(state) => state.thread.messages.length === 0}>
            <div className="flex flex-1 flex-col justify-center pb-[12vh]">
              <Welcome />
            </div>
          </AuiIf>

          <div className="mb-14 flex flex-col gap-8 empty:hidden">
            <ThreadPrimitive.Messages>{() => <ThreadMessage />}</ThreadPrimitive.Messages>
          </div>

          <ThreadPrimitive.ViewportFooter className="sticky bottom-0 mt-auto flex flex-col gap-2 bg-white pb-2">
            <ThreadPrimitive.ScrollToBottom asChild>
              <button
                type="button"
                className="absolute -top-12 self-center rounded-full border border-[#e5e5e5] bg-white p-2 shadow-sm disabled:invisible"
                aria-label="Ir al final"
              >
                <ArrowDownIcon className="size-4" aria-hidden />
              </button>
            </ThreadPrimitive.ScrollToBottom>
            <Composer autoFocus={autoFocus} />
            <p className="text-center text-xs text-[#6b6b6b]">
              Revisa los datos importantes antes de aprobar una compra.
            </p>
          </ThreadPrimitive.ViewportFooter>
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  )
}
