/// Genesis Prestige API transport.
///
/// Layering (P17 §3.1): only a feature's `data/` layer may import this
/// package. A `presentation/` import is an import-boundary violation and fails
/// `mobile:analyze`.
///
/// Request/response SHAPES are not defined here by hand — they are generated
/// into `src/generated/` from `web/packages/api-client/openapi.json`, the
/// binding contract, and `mobile:codegen-drift` fails the pipeline on any
/// hand-edit or stale snapshot. Only the transport, the token custody, the
/// pin set, the idempotency contract and the error envelope are written by
/// hand (scaffold defect D3).
library gp_api_client;

export 'src/infrastructure/certificate_pinning.dart';
export 'src/infrastructure/gp_http_client.dart';
export 'src/infrastructure/idempotency_key.dart';
export 'src/infrastructure/token_storage.dart';
export 'src/models/api_error.dart';
