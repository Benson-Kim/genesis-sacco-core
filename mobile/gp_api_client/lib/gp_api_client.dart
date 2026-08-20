/// Genesis Prestige API transport.
///
/// Layering (P17 §3.1): only a feature's `data/` layer may import this
/// package. A `presentation/` import is an import-boundary violation and fails
/// `mobile:analyze`.
///
/// Request/response SHAPES are not defined here by hand — they are generated
/// into `src/generated/` by `mobile/scripts/generate_api_models.py` from
/// `web/packages/api-client/openapi.json`, the binding contract, and
/// `mobile:codegen-drift` fails the pipeline on any hand-edit or stale
/// snapshot. Only the transport, the token custody, the pin set, the
/// idempotency contract and the error envelope are written by hand
/// (scaffold defect D3).
///
/// The generated file covers the `/member` surface ONLY. The snapshot holds
/// 194 schemas across 108 paths; emitting all of them would put every staff
/// shape one import away from a member screen, so the generator walks the
/// `/member` paths and their transitive references and stops there.
library gp_api_client;

export 'src/infrastructure/certificate_pinning.dart';
export 'src/infrastructure/gp_http_client.dart';
export 'src/infrastructure/idempotency_key.dart';
export 'src/infrastructure/token_storage.dart';
export 'src/models/api_error.dart';
export 'src/generated/member_models.dart';
