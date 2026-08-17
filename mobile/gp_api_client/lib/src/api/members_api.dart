import '../infrastructure/gp_http_client.dart';
import '../models/member_models.dart';

/// Members API — wraps /members/* endpoints.
class MembersApi {
  MembersApi(this._client);

  final GpHttpClient _client;

  /// Keyset-paginated member list (MASTER_PROMPT §1.3).
  Future<PagedMembers> listMembers({String? cursor, int limit = 20}) async {
    final json = await _client.get(
      '/members',
      queryParams: {
        if (cursor != null) 'cursor': cursor,
        'limit': limit.toString(),
      },
    );
    return PagedMembers.fromJson(json);
  }

  /// Fetch a single member by ID.
  Future<MemberSummary> getMember(String memberId) async {
    final json = await _client.get('/members/$memberId');
    return MemberSummary.fromJson(json);
  }
}
