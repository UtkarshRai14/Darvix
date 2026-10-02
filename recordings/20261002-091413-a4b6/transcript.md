# Call 20261002-091413-a4b6 - Health insurance lead qualification (India)

- Source: web | Started: 2026-10-02T03:44:13+00:00 | Duration: 204.8 s
- Outcome: **Qualified - advisor follow-up** (all required details captured; no declared conditions)
- End reason: submit | Actions: lead_created
- CRM summary (CAL-d948e3c4): Health insurance lead qualification call (India). Outcome: Qualified - advisor follow-up. Reasons: all required details captured; no declared conditions. Captured - name: Utkars life, age: 21, city: Sonipat, members to cover: self, eldest member's age: 21, pre-existing conditions: none, sum insured: 5, consent to be contacted: yes, callback time: tomorrow afternoon. Suggested plan: Essential. Actions: lead_created.
- Flags: fallback used: answer_status is answered_from_kb but no record_id from KNOW
- Latency p50 (ms): STT 283, retrieval 28, LLM 973, TTS 1751, total 2886

## Transcript

**[   0.0s] Agent:** Good morning! This is Ananya, a virtual assistant from Nivaran Health Insurance. This call is recorded for quality and training. You had asked about health insurance on our website. Is this a good time to talk for about three minutes?  
**[  22.8s] Customer:** Yes, I am available.  
**[  25.0s] Agent:** May I have your full name, please?  
**[  31.0s] Customer:** Of course, right.  
**[  33.6s] Agent:** Could you please tell me your full name?  
**[  41.0s] Customer:** Utkars life.  
**[  45.3s] Agent:** Thank you, Utkars life. May I know your age, please?  
**[  54.4s] Customer:** 21.  
**[  56.7s] Agent:** Which city do you live in?  
**[  62.9s] Customer:** Sorry, but  
**[  64.8s] Agent:** I’m sorry, could you please tell me which city you live in?  
**[  71.4s] Customer:** Sonipat.  
**[  85.9s] Agent:** Great, thank you. Who would you like to cover under the plan – yourself, your spouse, children, or parents?  
**[  97.8s] Customer:** Only myself.  
**[  99.8s] Agent:** Do you have any pre‑existing medical conditions, or should I note “none”?  
**[ 107.9s] Customer:** Note 9.  
**[ 111.6s] Agent:** Could you please confirm if you have any pre‑existing medical conditions, or should I record “none”?  
**[ 120.2s] Customer:** None.  
**[ 122.7s] Agent:** I'm sorry, I don't have verified information on that. I can ask a licensed advisor to call you back with the exact details. *(info unavailable)*  
**[ 137.5s] Customer:** Okay.  
**[ 139.7s] Agent:** What sum insured would you like, in lakh rupees?  
**[ 147.6s] Customer:** Five lakh rupees.  
**[ 151.1s] Agent:** For a five lakh rupees sum insured, the indicative annual premium for an Essential plan is about five thousand six hundred rupees, excluding GST. Do you consent to be contacted by a licensed advisor, and what day and time would be best for a callback? `[kb_product_ess_003]` `[kb_pricing_prem_001]`  
**[ 171.6s] Customer:** Maybe tomorrow afternoon.  
**[ 176.6s] Agent:** May I have your consent to be contacted by a licensed advisor, and confirm tomorrow afternoon as the preferred callback time?  
**[ 187.3s] Customer:** Yes.  
**[ 190.7s] Agent:** Thank you. I have noted your consent and the callback for tomorrow afternoon. A licensed advisor will call you then to discuss the Essential plan further. Have a great day! `[kb_policy_ess_005]` *(action: submit)*  

## Knowledge-base citations

- `kb_product_ess_003`: data/raw/health_in/website/essential.html > Sum insured options (v1.0)
- `kb_pricing_prem_001`: data/raw/health_in/tables/indicative-premium-chart.csv > Indicative annual premium - Essential, 1 adult (v1.0)
- `kb_policy_ess_005`: data/raw/health_in/website/essential.html > Waiting periods (v1.0)
