/// Genesis Prestige API client package.
///
/// This package is generated from the backend OpenAPI spec.
/// Run `scripts/regenerate_api_client.sh` to regenerate after spec changes.
/// Do NOT hand-edit files under lib/src/generated/ (MASTER_PROMPT §1.1).
///
/// The hand-written infrastructure layer (token storage, cert pinning,
/// HTTP client) lives in lib/src/infrastructure/ and is NOT generated.
library gp_api_client;

export 'src/api/auth_api.dart';
export 'src/api/members_api.dart';
export 'src/infrastructure/certificate_pinning.dart';
export 'src/infrastructure/gp_http_client.dart';
export 'src/infrastructure/token_storage.dart';
export 'src/models/api_error.dart';
export 'src/models/auth_models.dart';
export 'src/models/member_models.dart';
