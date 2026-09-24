import { createServer } from "node:http"
import { toNodeHandler } from "better-auth/node"
import { getMigrations } from "better-auth/db/migration"
import { auth } from "./auth.js"

const { runMigrations } = await getMigrations(auth.options)
await runMigrations()

const port = Number(process.env.PORT ?? 4210)
createServer(toNodeHandler(auth)).listen(port, "0.0.0.0")
