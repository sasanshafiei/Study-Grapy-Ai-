-- F03 grants only. Run as the trusted owner after 0009 on the intended database.
-- CE-3 provisions these separate, unprivileged group roles and login memberships.
-- No passwords, group creation, cluster-wide changes or PUBLIC grants here.
GRANT USAGE ON SCHEMA public TO studygraph_ai_indexer, studygraph_ai_retriever, studygraph_ce_runtime;
GRANT EXECUTE ON FUNCTION public.commit_index_generation(uuid,uuid,text,text,integer,text,text,jsonb)
    TO studygraph_ai_indexer;
GRANT EXECUTE ON FUNCTION public.retrieve_index_chunks(uuid,text[]) TO studygraph_ai_retriever;
GRANT SELECT ON public.index_generations TO studygraph_ce_runtime;
GRANT EXECUTE ON FUNCTION public.verified_index_generation(uuid,uuid,text,text,integer,integer) TO studygraph_ce_runtime;
