"""Reuse the isolated real-Vault ACL fixture with the dedicated visitor policy.

KV field filtering is a VSO contract, not a Vault path-policy capability.
The shared fixture uses synthetic fields only; no production credentials enter it.
"""
import argparse
import json
import community_youtube_integration as fixture_module

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-isolated',action='store_true')
    args=parser.parse_args()
    fixture_module.require(args.run_isolated,'--run-isolated is required')
    fixture_module.POLICY_NAME='community-visits-read'
    fixture_module.SECRET_PATH='pawbridge/prod/community/visits'
    fixture_module.LABEL='pawbridge.test.daily-visitors-acl'
    fixture=fixture_module.IsolatedVault()
    fixture.policy=(fixture_module.ROOT/'infra/vault/policies/community-visits-read.hcl').read_text()
    try:
        fixture.start(); fixture.token_tests()
        print(json.dumps({'result':'passed','checks':fixture.checks,'valuesPrinted':False,
            'scope':'dedicated_visitor_policy_real_vault_acl_not_production_vso'}))
    finally:
        fixture.close()

if __name__=='__main__': main()
